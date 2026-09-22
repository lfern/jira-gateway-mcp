"""
Todo lo que este módulo NO expone, el agente no puede hacerlo: nada de
merge, approve, decline ni push de PRs; nada de parar, cancelar ni relanzar
pipelines. Solo crear/leer PRs y lanzar/leer pipelines custom que ya estén
en su allowlist (confirmación en dos pasos en server.py para lo que
escribe).
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

        `reviewers`, si se pasa, es una lista de UUIDs de cuenta Atlassian
        (formato Bitbucket: {"uuid": "..."}). Si se omite, no se manda el
        campo y Bitbucket aplica los default reviewers configurados en el
        repo."""
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
            payload["reviewers"] = [{"uuid": r} for r in reviewers]

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
        }

    def list_open_pull_requests(self, repo_slug: str) -> list[dict]:
        """Solo lectura. Devuelve como mucho los primeros 50 PR abiertos
        (límite de página de Bitbucket) — suficiente para el uso normal de
        un repo de equipo."""
        validate_repo_slug(repo_slug, self.cfg)

        resp = requests.get(
            f"{self._repo_url(repo_slug)}/pullrequests",
            headers=self._headers(),
            params={"state": "OPEN", "pagelen": 50},
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
            "url": pr["links"]["html"]["href"],
            "created_on": pr.get("created_on"),
            "updated_on": pr.get("updated_on"),
        }

    def find_open_pr_for_branch(self, repo_slug: str, source_branch: str) -> dict | None:
        """Usado para no duplicar PR: busca entre los PR abiertos uno cuya
        rama origen coincida exactamente."""
        validate_repo_slug(repo_slug, self.cfg)
        validate_branch_name(source_branch)

        for pr in self.list_open_pull_requests(repo_slug):
            if pr["source"] == source_branch:
                return pr
        return None

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
