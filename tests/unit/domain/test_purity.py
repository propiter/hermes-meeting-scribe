"""DESIGN §1.1: ``domain/`` imports nothing outside the stdlib and itself."""
import ast
import sys
from pathlib import Path

DOMAIN = Path(__file__).resolve().parents[3] / "meeting_scribe" / "domain"


def test_domain_imports_only_stdlib():
    offenders = []
    for py in DOMAIN.glob("*.py"):
        for node in ast.walk(ast.parse(py.read_text())):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                if root not in sys.stdlib_module_names and root != "__future__":
                    offenders.append(f"{py.name}: {name}")
    assert offenders == []
