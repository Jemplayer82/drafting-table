from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _read_dockerignore_lines() -> list[str]:
    return DOCKERIGNORE.read_text(encoding="utf-8").splitlines()


def test_dockerignore_exists() -> None:
    assert DOCKERIGNORE.is_file(), f".dockerignore should exist as a file at {DOCKERIGNORE}"


REQUIRED_LINES = [
    ".git",
    ".github",
    ".venv",
    ".env",
    ".env.*",
    "!.env.example",
    "__pycache__/",
    ".pytest_cache/",
    ".ruff_cache/",
    "data/",
    "media/",
    ".pipeline/",
    "node_modules/",
]


def test_dockerignore_has_required_lines() -> None:
    lines = _read_dockerignore_lines()
    for line in REQUIRED_LINES:
        assert line in lines, f"Required line {line!r} missing from .dockerignore"


def test_dockerignore_negation_comes_after_env_patterns() -> None:
    lines = _read_dockerignore_lines()

    negation = "!.env.example"
    assert negation in lines, f"Required negation line {negation!r} missing from .dockerignore"
    negation_index = lines.index(negation)

    for pattern in (".env", ".env.*"):
        assert pattern in lines, f"Required pattern line {pattern!r} missing from .dockerignore"
        pattern_index = lines.index(pattern)

        assert negation_index > pattern_index, (
            f"{negation!r} must come after {pattern!r} in .dockerignore. "
            "Docker applies ignore patterns in order, so a negation (!) only "
            "re-includes a file if it appears after the exclusion pattern. "
            "If .env.example is listed before .env/.env.*, .env.example is "
            "silently excluded from the Docker build context again."
        )
