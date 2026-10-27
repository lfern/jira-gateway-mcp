"""
Todo lo que este módulo NO expone, el agente no puede hacerlo: nada de
merge, approve, decline ni push de PRs; nada de parar, cancelar ni relanzar
pipelines. Solo crear/leer PRs (con revisores explícitos opcionales) y
lanzar/leer pipelines custom que ya estén en su allowlist (confirmación en
dos pasos en server.py para lo que escribe).
"""
import re

import requests

from .config import Config

# Regex conservadora para nombres de rama: empieza por alfanumérico, sigue
# con alfanumérico/._- y '/' (para prefijos tipo feature/REF-432-x). Los
# controles adicionales (sin "..", sin barra final, sin dobles barras) se
# hacen aparte porque son más legibles fuera de la regex.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")

# Nombre de pipeline custom (clave en `custom:` de bitbucket-pipelines.yml):
# más restrictivo que un nombre de rama, no necesita '/'.
_PIPELINE_PATTERN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")

# Revisor tal como lo pasa el agente: account_id (ej. "557058:abc-..."),
# {uuid} o nickname. Nunca va en una URL (se compara contra los miembros del
# workspace), pero se acota igual para no arrastrar basura a los mensajes.
_REVIEWER_RE = re.compile(r"^[A-Za-z0-9{}:._@+-]{1,128}$")
_MAX_REVIEWERS = 10
# Tope de páginas al listar miembros/default reviewers (100 por página).
_MAX_PAGES = 20


class BitbucketError(Exception):
    pass


def validate_repo_slug(repo_slug: str, cfg: Config) -> None:
    if not cfg.bitbucket_allowed_repos:
        raise BitbucketError(
            "no hay repos permitidos configurados (BITBUCKET_ALLOWED_REPOS "
            "vacío en el .env del servicio)."
        )
    if repo_slug not in cfg.bitbucket_allowed_repos:
        raise BitbucketError(
            f"repo no permitido: {repo_slug!r}. Permitidos: {list(cfg.bitbucket_allowed_repos)}"
        )


def validate_branch_name(name: str) -> None:
    if not name or not _BRANCH_RE.match(name):
        raise BitbucketError(f"nombre de rama con formato inválido: {name!r}")
    if ".." in name or "//" in name or name.endswith("/") or name.endswith(".lock"):
        raise BitbucketError(f"nombre de rama con formato inválido: {name!r}")


def validate_pr_id(pr_id: int) -> int:
    if isinstance(pr_id, bool) or not isinstance(pr_id, int) or pr_id <= 0:
        raise BitbucketError(f"pr_id inválido: {pr_id!r}")
    return pr_id


def validate_pipeline_pattern(pattern: str, cfg: Config) -> None:
    if not cfg.bitbucket_allowed_pipelines:
        raise BitbucketError(
            "no hay pipelines permitidos configurados (BITBUCKET_ALLOWED_PIPELINES "
            "vacío en el .env del servicio)."
        )
    if not pattern or not _PIPELINE_PATTERN_RE.match(pattern):
        raise BitbucketError(f"nombre de pipeline con formato inválido: {pattern!r}")
    if pattern not in cfg.bitbucket_allowed_pipelines:
        raise BitbucketError(
            f"pipeline no permitido: {pattern!r}. Permitidos: {list(cfg.bitbucket_allowed_pipelines)}"
        )


def validate_ref_type(ref_type: str) -> None:
    if ref_type not in ("branch", "tag"):
        raise BitbucketError(f"ref_type inválido: {ref_type!r} (debe ser 'branch' o 'tag')")


def validate_build_number(build_number: int) -> int:
    if isinstance(build_number, bool) or not isinstance(build_number, int) or build_number <= 0:
        raise BitbucketError(f"build_number inválido: {build_number!r}")
    return build_number


def normalize_reviewers(reviewers: list[str] | None) -> list[str]:
    """Limpia la lista que pasa el agente: quita espacios, vacíos y
    repetidos (conservando el orden) y valida el formato. El email no se
    admite: Bitbucket no expone el email de otros usuarios, así que no hay
    contra qué resolverlo."""
    result: list[str] = []
    for raw in reviewers or []:
        if not isinstance(raw, str):
            raise BitbucketError(f"revisor inválido: {raw!r}")
        value = raw.strip()
        if not value or value in result:
            continue
        if "@" in value:
            raise BitbucketError(
                f"revisor {value!r}: Bitbucket no permite buscar usuarios por email. "
                "Usa su account_id, su {uuid} o su nickname."
            )
        if not _REVIEWER_RE.match(value):
            raise BitbucketError(f"revisor con formato inválido: {value!r}")
        result.append(value)
    if len(result) > _MAX_REVIEWERS:
        raise BitbucketError(f"demasiados revisores ({len(result)}); máximo {_MAX_REVIEWERS}.")
    return result


def _compact_user(user: dict) -> dict:
    return {"display_name": user.get("display_name", "?"), "account_id": user.get("account_id")}


def _raise_for_status(resp: requests.Response, action: str) -> None:
    if resp.ok:
        return
    try:
        body = resp.json()
        message = body.get("error", {}).get("message") or resp.text[:300]
    except ValueError:
        message = resp.text[:300]
    raise BitbucketError(f"Bitbucket {resp.status_code} al {action}: {message}")


def _raise_for_pipeline_status(resp: requests.Response, action: str) -> None:
    """Como _raise_for_status, pero con pista explícita en 401/403: el fallo
    más habitual al añadir Pipelines es que el token siga sin ese scope."""
    if resp.status_code in (401, 403):
        raise BitbucketError(
            f"Bitbucket {resp.status_code} al {action}: el token no tiene permiso "
            "para Pipelines (probablemente falta el scope de escritura de "
            "Pipelines en BITBUCKET_API_TOKEN). Revisa los scopes del token en "
            "id.atlassian.com y actualízalo en el .env del servicio."
        )
    _raise_for_status(resp, action)


def _raise_for_scope_status(resp: requests.Response, action: str, scope: str) -> None:
    """Como _raise_for_pipeline_status, para las lecturas que solo hacen
    falta al pedir revisores explícitos (miembros, usuario actual)."""
    if resp.status_code in (401, 403):
        raise BitbucketError(
            f"Bitbucket {resp.status_code} al {action}: el token no tiene permiso "
            f"(probablemente falta el scope {scope} en BITBUCKET_API_TOKEN). Revisa "
            "los scopes del token en id.atlassian.com y actualízalo en el .env del "
            "servicio."
        )
    _raise_for_status(resp, action)


class BitbucketClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _headers(self) -> dict:
        return {"Authorization": self.cfg.bitbucket_auth_header, "Content-Type": "application/json"}

    def _repo_url(self, repo_slug: str) -> str:
        return f"{self.cfg.bitbucket_api_base}/repositories/{self.cfg.bitbucket_workspace}/{repo_slug}"

    def _compact_pr(self, pr: dict) -> dict:
        return {
            "id": pr["id"],
            "title": pr["title"],
            "source": pr["source"]["branch"]["name"],
            "destination": pr["destination"]["branch"]["name"],
            "author": pr.get("author", {}).get("display_name", "?"),
            "state": pr.get("state"),
            "url": pr["links"]["html"]["href"],
        }

    def create_pull_request(
        self,
        repo_slug: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        close_source_branch: bool,
        reviewers: list[str] | None = None,
    ) -> dict:
        """Crea el PR de verdad. Requiere que el llamante ya haya
        confirmado y comprobado que no hay uno abierto ya para esa rama —
        esta función no pregunta nada, solo ejecuta.

        `reviewers`, si se pasa, es la lista FINAL de account_ids ya
        resueltos con resolve_reviewers (explícitos + default reviewers):
        Bitbucket toma ese campo tal cual. Si se omite, no se manda el campo
        y Bitbucket aplica los default reviewers configurados en el repo."""
        validate_repo_slug(repo_slug, self.cfg)
        validate_branch_name(source_branch)
        validate_branch_name(target_branch)

        payload = {
            "title": title,
            "description": description,
            "source": {"branch": {"name": source_branch}},
            "destination": {"branch": {"name": target_branch}},
            "close_source_branch": close_source_branch,
        }
        if reviewers:
            payload["reviewers"] = [{"account_id": r} for r in reviewers]

        resp = requests.post(
            f"{self._repo_url(repo_slug)}/pullrequests",
            headers=self._headers(),
            json=payload,
            timeout=15,
        )
        _raise_for_status(resp, f"crear PR en {repo_slug}")

        pr = resp.json()
        return {
            "id": pr["id"],
            "url": pr["links"]["html"]["href"],
            "state": pr.get("state"),
            "source": pr["source"]["branch"]["name"],
            "destination": pr["destination"]["branch"]["name"],
            "reviewers": [_compact_user(r) for r in pr.get("reviewers", [])],
        }

    def list_open_pull_requests(self, repo_slug: str, source_branch: str | None = None) -> list[dict]:
        """Solo lectura. Devuelve como mucho los primeros 50 PR abiertos
        (límite de página de Bitbucket) — suficiente para el uso normal de
        un repo de equipo. Con `source_branch`, el filtro lo hace Bitbucket
        (BBQL), así que no le afecta ese límite. Ojo: con `q`, Bitbucket
        ignora el parámetro `state`, así que el estado va también dentro de
        la query. El nombre de rama ya viene validado por _BRANCH_RE (sin
        comillas), así que no puede romper la query."""
        validate_repo_slug(repo_slug, self.cfg)
        params = {"state": "OPEN", "pagelen": 50}
        if source_branch is not None:
            validate_branch_name(source_branch)
            params["q"] = f'source.branch.name = "{source_branch}" AND state = "OPEN"'

        resp = requests.get(
            f"{self._repo_url(repo_slug)}/pullrequests",
            headers=self._headers(),
            params=params,
            timeout=15,
        )
        _raise_for_status(resp, f"listar PRs de {repo_slug}")

        return [self._compact_pr(pr) for pr in resp.json().get("values", [])]

    def get_pull_request(self, repo_slug: str, pr_id: int) -> dict:
        """Solo lectura."""
        validate_repo_slug(repo_slug, self.cfg)
        pr_id = validate_pr_id(pr_id)

        resp = requests.get(
            f"{self._repo_url(repo_slug)}/pullrequests/{pr_id}",
            headers=self._headers(),
            timeout=15,
        )
        _raise_for_status(resp, f"leer PR {pr_id} de {repo_slug}")

        pr = resp.json()
        return {
            "id": pr["id"],
            "title": pr["title"],
            "description": pr.get("description", ""),
            "state": pr.get("state"),
            "source": pr["source"]["branch"]["name"],
            "destination": pr["destination"]["branch"]["name"],
            "author": pr.get("author", {}).get("display_name", "?"),
            "close_source_branch": pr.get("close_source_branch", False),
            "reviewers": [_compact_user(r) for r in pr.get("reviewers", [])],
            "url": pr["links"]["html"]["href"],
            "created_on": pr.get("created_on"),
            "updated_on": pr.get("updated_on"),
        }

    def find_open_pr_for_branch(self, repo_slug: str, source_branch: str) -> dict | None:
        """Usado para no duplicar PR: busca entre los PR abiertos uno cuya
        rama origen coincida exactamente."""
        validate_repo_slug(repo_slug, self.cfg)
        validate_branch_name(source_branch)

        for pr in self.list_open_pull_requests(repo_slug, source_branch):
            if pr["source"] == source_branch and pr["state"] == "OPEN":
                return pr
        return None

    def _get_all_pages(self, url: str, action: str, scope: str) -> list[dict]:
        values: list[dict] = []
        params: dict | None = {"pagelen": 100}
        for _ in range(_MAX_PAGES):
            resp = requests.get(url, headers=self._headers(), params=params, timeout=15)
            _raise_for_scope_status(resp, action, scope)
            body = resp.json()
            values.extend(body.get("values", []))
            url = body.get("next")
            if not url:
                break
            params = None  # `next` ya trae la query completa
        return values

    def list_workspace_members(self) -> list[dict]:
        """Solo lectura. Usuarios (display_name, account_id, uuid,
        nickname) del workspace configurado."""
        members = self._get_all_pages(
            f"{self.cfg.bitbucket_api_base}/workspaces/{self.cfg.bitbucket_workspace}/members",
            "listar los miembros del workspace",
            "read:workspace:bitbucket",
        )
        return [m["user"] for m in members if m.get("user")]

    def get_current_user(self) -> dict:
        """Solo lectura. El dueño del token, que es quien firma los PR."""
        resp = requests.get(f"{self.cfg.bitbucket_api_base}/user", headers=self._headers(), timeout=15)
        _raise_for_scope_status(resp, "leer tu usuario de Bitbucket", "read:user:bitbucket")
        return resp.json()

    def list_default_reviewers(self, repo_slug: str) -> list[dict]:
        """Solo lectura. Default reviewers efectivos del repo (los del repo
        y los heredados del proyecto)."""
        validate_repo_slug(repo_slug, self.cfg)
        entries = self._get_all_pages(
            f"{self._repo_url(repo_slug)}/effective-default-reviewers",
            f"leer los default reviewers de {repo_slug}",
            "read:pullrequest:bitbucket",
        )
        return [e["user"] for e in entries if e.get("user")]

    def resolve_reviewers(self, repo_slug: str, reviewers: list[str]) -> dict:
        """Solo lectura. Convierte lo que pasa el agente (account_id, {uuid}
        o nickname) en usuarios del workspace y calcula la lista final para
        el PR. Lanza BitbucketError, sin crear nada, si algún revisor no
        existe o está fuera de BITBUCKET_ALLOWED_REVIEWERS.

        Devuelve:
        - `reviewers`: los explícitos ya resueltos (sin el autor).
        - `default_reviewers`: los default del repo que se suman (sin el
          autor ni los ya explícitos).
        - `account_ids`: la unión, en ese orden, para el POST. Al mandar el
          campo `reviewers`, Bitbucket no añade los default por su cuenta,
          así que van aquí para que se sigan sumando.
        - `warnings`: avisos para la respuesta (ej. autor quitado)."""
        validate_repo_slug(repo_slug, self.cfg)
        wanted = normalize_reviewers(reviewers)

        index: dict[str, dict] = {}
        for user in self.list_workspace_members():
            for key in (user.get("account_id"), user.get("uuid"), user.get("nickname")):
                if key:
                    index.setdefault(key.lower(), user)

        resolved: list[dict] = []
        unknown: list[str] = []
        for value in wanted:
            key = value.lower()
            if key not in index and not key.startswith("{"):
                key = "{" + key + "}"  # uuid pasado sin llaves
            user = index.get(key)
            if user is None:
                unknown.append(value)
            elif all(u.get("account_id") != user.get("account_id") for u in resolved):
                resolved.append(user)
        if unknown:
            raise BitbucketError(
                f"revisor(es) no encontrado(s) en el workspace {self.cfg.bitbucket_workspace}: "
                f"{unknown}. Usa su account_id, su {{uuid}} o su nickname."
            )

        allowed = self.cfg.bitbucket_allowed_reviewers
        if allowed:
            rejected = [
                f"{u.get('display_name', '?')} ({u.get('account_id')})"
                for u in resolved
                if u.get("account_id") not in allowed
            ]
            if rejected:
                raise BitbucketError(
                    f"revisor(es) no permitido(s): {rejected}. Solo se aceptan los de "
                    "BITBUCKET_ALLOWED_REVIEWERS."
                )

        warnings: list[str] = []
        author_id = self.get_current_user().get("account_id")
        if any(u.get("account_id") == author_id for u in resolved):
            resolved = [u for u in resolved if u.get("account_id") != author_id]
            warnings.append(
                "Se ha quitado al autor del PR de la lista de revisores: Bitbucket no "
                "permite que el autor revise su propio PR."
            )

        explicit_ids = {u.get("account_id") for u in resolved}
        defaults: list[dict] = []
        for user in self.list_default_reviewers(repo_slug):
            account_id = user.get("account_id")
            if account_id and account_id != author_id and account_id not in explicit_ids:
                if all(d.get("account_id") != account_id for d in defaults):
                    defaults.append(user)

        return {
            "reviewers": [_compact_user(u) for u in resolved],
            "default_reviewers": [_compact_user(u) for u in defaults],
            "account_ids": [u["account_id"] for u in resolved + defaults],
            "warnings": warnings,
        }

    def run_pipeline(self, repo_slug: str, pattern: str, ref_name: str, ref_type: str) -> dict:
        """Lanza el pipeline custom de verdad. Requiere que el llamante ya
        haya confirmado — esta función no pregunta nada, solo ejecuta. NUNCA
        para, cancela ni relanza pipelines: solo dispara uno nuevo cada vez
        que se llama."""
        validate_repo_slug(repo_slug, self.cfg)
        validate_pipeline_pattern(pattern, self.cfg)
        validate_ref_type(ref_type)
        validate_branch_name(ref_name)

        payload = {
            "target": {
                "type": "pipeline_ref_target",
                "ref_type": ref_type,
                "ref_name": ref_name,
                "selector": {"type": "custom", "pattern": pattern},
            }
        }
        resp = requests.post(
            f"{self._repo_url(repo_slug)}/pipelines/",
            headers=self._headers(),
            json=payload,
            timeout=15,
        )
        _raise_for_pipeline_status(resp, f"lanzar el pipeline {pattern!r} en {repo_slug}")

        pipeline = resp.json()
        build_number = pipeline["build_number"]
        return {
            "build_number": build_number,
            "uuid": pipeline["uuid"],
            "state": (pipeline.get("state") or {}).get("name"),
            "url": (
                f"https://bitbucket.org/{self.cfg.bitbucket_workspace}/"
                f"{repo_slug}/pipelines/results/{build_number}"
            ),
        }

    def get_pipeline(self, repo_slug: str, build_number: int) -> dict:
        """Solo lectura."""
        validate_repo_slug(repo_slug, self.cfg)
        build_number = validate_build_number(build_number)

        resp = requests.get(
            f"{self._repo_url(repo_slug)}/pipelines/{build_number}",
            headers=self._headers(),
            timeout=15,
        )
        _raise_for_pipeline_status(resp, f"leer el pipeline {build_number} de {repo_slug}")

        pipeline = resp.json()
        state = pipeline.get("state") or {}
        return {
            "build_number": pipeline["build_number"],
            "uuid": pipeline["uuid"],
            "state": state.get("name"),
            "result": (state.get("result") or {}).get("name"),
            "url": (
                f"https://bitbucket.org/{self.cfg.bitbucket_workspace}/"
                f"{repo_slug}/pipelines/results/{pipeline['build_number']}"
            ),
        }
