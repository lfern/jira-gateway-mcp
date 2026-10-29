"""
Bandeja de adjuntos: la única carpeta de la que el gateway sube ficheros a
Jira.

Las tools nunca aceptan rutas, solo nombres de fichero dentro de esta
carpeta. Si aceptaran rutas, en modo servicio el gateway podría subir a
Jira ficheros que el agente no puede leer, empezando por su propio .env
con los tokens. Por eso aquí:

- el nombre no puede tener barras, ni empezar por punto, ni salirse de la
  carpeta;
- el fichero se abre relativo a la carpeta y con O_NOFOLLOW (sin enlaces
  simbólicos), y tiene que ser un fichero normal con un solo enlace (sin
  hard links a otra parte);
- solo se admiten unas extensiones concretas y un tamaño máximo.
"""
import hashlib
import os
import re
import stat
from dataclasses import dataclass

# Letras (con tildes), dígitos, espacios y poca puntuación. Sin barras y sin
# punto inicial, así que no puede ser una ruta, ni "..", ni un fichero oculto.
_FILENAME_RE = re.compile(r"^[^\W_][\w .()-]{0,127}$")

# Extensión -> tipo MIME. Lista fija a propósito: capturas, PDFs y logs.
ALLOWED_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".log": "text/plain",
}

MAX_FILES_PER_CALL = 10


class AttachmentError(Exception):
    pass


@dataclass
class InboxFile:
    filename: str
    content: bytes
    mime_type: str
    sha256: str

    @property
    def size(self) -> int:
        return len(self.content)

    def summary(self) -> dict:
        return {
            "filename": self.filename,
            "size": self.size,
            "mime_type": self.mime_type,
            "sha256": self.sha256,
        }


def inbox_from_config(directory: str | None, max_mb: int, windows_dir: str | None) -> "Inbox | None":
    if not directory:
        return None
    if not windows_dir and os.environ.get("WSL_DISTRO_NAME"):
        # En WSL, para que el usuario abra el fichero con un clic desde Windows.
        windows_dir = f"\\\\wsl.localhost\\{os.environ['WSL_DISTRO_NAME']}" + directory.replace("/", "\\")
    return Inbox(directory, max_mb * 1024 * 1024, windows_dir)


def validate_filename(name: str) -> str:
    if not _FILENAME_RE.match(name or ""):
        raise AttachmentError(
            f"nombre de fichero no válido: {name!r}. Solo el nombre (sin rutas), "
            "sin punto inicial, con letras, dígitos, espacios y . ( ) - _"
        )
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_TYPES:
        allowed = ", ".join(sorted(ALLOWED_TYPES))
        raise AttachmentError(f"tipo de fichero no permitido: {name!r}. Permitidos: {allowed}")
    return name


def validate_filenames(names: list[str]) -> list[str]:
    if not names:
        raise AttachmentError("indica al menos un fichero")
    if len(names) > MAX_FILES_PER_CALL:
        raise AttachmentError(f"máximo {MAX_FILES_PER_CALL} ficheros por llamada")
    if len(set(names)) != len(names):
        raise AttachmentError("hay ficheros repetidos en la lista")
    return [validate_filename(n) for n in names]


class Inbox:
    def __init__(self, directory: str, max_bytes: int, windows_dir: str | None = None):
        self.directory = directory
        self.max_bytes = max_bytes
        self.windows_dir = windows_dir

    def path(self, filename: str) -> str:
        return os.path.join(self.directory, filename)

    def windows_path(self, filename: str) -> str | None:
        if not self.windows_dir:
            return None
        return self.windows_dir.rstrip("\\") + "\\" + filename

    def _open_dir(self) -> int:
        try:
            return os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as e:
            raise AttachmentError(f"no puedo abrir la bandeja {self.directory}: {e.strerror}")

    def list(self) -> list[dict]:
        """Ficheros de la bandeja que se podrían subir (los demás se ignoran)."""
        dir_fd = self._open_dir()
        try:
            names = sorted(os.listdir(dir_fd))
        finally:
            os.close(dir_fd)
        files = []
        for name in names:
            try:
                validate_filename(name)
            except AttachmentError:
                continue
            try:
                st = os.stat(os.path.join(self.directory, name), follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                files.append({"filename": name, "size": st.st_size, "windows_path": self.windows_path(name)})
        return files

    def read(self, filename: str) -> InboxFile:
        validate_filename(filename)
        dir_fd = self._open_dir()
        try:
            try:
                # O_NONBLOCK: abrir un FIFO no se queda esperando (luego se
                # rechaza por no ser un fichero normal).
                fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
            except FileNotFoundError:
                raise AttachmentError(
                    f"{filename} no está en la bandeja {self.directory}. Cópialo antes con: "
                    f"install -m 644 <ruta>/{filename} {self.directory}/"
                )
            except PermissionError:
                raise AttachmentError(
                    f"no puedo leer {filename}: dale permiso de lectura "
                    f"(chmod g+r {self.path(filename)})"
                )
            except OSError as e:
                # ELOOP: es un enlace simbólico.
                raise AttachmentError(f"no puedo abrir {filename} (¿enlace simbólico?): {e.strerror}")
        finally:
            os.close(dir_fd)

        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise AttachmentError(f"{filename} no es un fichero normal")
            if st.st_nlink != 1:
                raise AttachmentError(f"{filename} tiene varios enlaces (hard link): no se sube")
            if st.st_size > self.max_bytes:
                raise AttachmentError(
                    f"{filename} ocupa {st.st_size} bytes, más que el máximo ({self.max_bytes})"
                )
        except AttachmentError:
            os.close(fd)
            raise
        with os.fdopen(fd, "rb") as f:
            content = f.read(self.max_bytes + 1)
        if len(content) > self.max_bytes:
            raise AttachmentError(f"{filename} supera el máximo de {self.max_bytes} bytes")
        if not content:
            raise AttachmentError(f"{filename} está vacío")

        ext = os.path.splitext(filename)[1].lower()
        return InboxFile(filename, content, ALLOWED_TYPES[ext], hashlib.sha256(content).hexdigest())

    def remove(self, filename: str) -> None:
        validate_filename(filename)
        dir_fd = self._open_dir()
        try:
            os.unlink(filename, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
