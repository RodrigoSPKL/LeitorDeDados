"""Package source and fictitious examples, never real databases or secrets."""
from pathlib import Path
import zipfile


def package(root: Path, destination: Path) -> int:
    root = root.resolve()
    roots = {"leitor_dados", "config", "examples", "scripts", "docs", "tests"}
    root_files = {"README.md", "SECURITY.md", "Dockerfile", "compose.yaml", ".dockerignore", ".gitignore", ".gitattributes",
                  "requirements.txt", "requirements.lock.txt", "requirements-dev.txt", "processar-pasta.sh"}
    allowed = {".py", ".json", ".jsonl", ".csv", ".sh", ".md", ".html"}
    candidates = [root / name for name in root_files]
    for directory in roots:
        candidates.extend((root / directory).rglob("*"))
    files = []
    for path in candidates:
        relative = path.relative_to(root)
        # Prune by allowlist before opening any file; no credentials/data are read.
        if relative.parts[0] not in roots and relative.as_posix() not in root_files:
            continue
        if path.is_symlink() or not path.is_file() or "__pycache__" in relative.parts:
            continue
        if relative.parts[0] in roots and path.suffix not in allowed:
            continue
        files.append((relative.as_posix(), path))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(files):
            info = zipfile.ZipInfo("leitor-dados/" + name, date_time=(2026, 11, 14, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100755 if name.endswith(".sh") else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return len(files)


if __name__ == "__main__":
    project = Path(__file__).resolve().parents[1]
    output = project / "dist" / "leitor-dados-linux.zip"
    count = package(project, output)
    print(f"{output.name}: {count} arquivos de código, documentação e exemplos fictícios.")
