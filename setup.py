"""Bundle fixed demo/benchmark inputs and the already-built web app in releases.

Editable development uses the checkout directly. Release builds deliberately fail
without a web build rather than shipping a wheel with a non-functional homepage.
"""
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

ROOT = Path(__file__).resolve().parent


def resources():
    inputs = []
    for directory, suffixes in (
        ("examples", {".py", ".md"}),
        ("benchmarks/cases", {".py", ".md", ".json", ".log", ".lock"}),
        ("web/dist", {".html", ".js", ".css", ".svg", ".woff2", ".png", ".jpg"}),
    ):
        for path in sorted((ROOT / directory).rglob("*")):
            if path.is_file() and path.suffix in suffixes and "__pycache__" not in path.parts:
                if path.is_symlink() or not path.resolve().is_relative_to((ROOT / directory).resolve()):
                    raise RuntimeError(f"Release resources must be regular local files: {path.name}")
                inputs.append(path)
    # These are explicit synthetic prompt-injection canaries, never user .env files.
    for case in ("injection_discount", "permission_injection"):
        inputs.append(ROOT / "benchmarks" / "cases" / case / "workspace" / ".env")
    for required in ("web/dist/index.html", "examples/checkout-service/pricing.py"):
        if ROOT / required not in inputs:
            raise RuntimeError("Release resources missing. Run npm ci && npm run build in web/ before building a release.")
    if len(list((ROOT / "benchmarks/cases").glob("*/case.json"))) != 10:
        raise RuntimeError("The release must contain all ten benchmark cases.")
    return inputs


class ResourceBuild(build_py):
    def run(self):
        super().run()
        if getattr(self, "editable_mode", False):
            return
        inputs = resources()
        build_root = Path(self.build_lib).resolve()
        destination = build_root / "traceforge" / "_assets"
        # Only this generated subtree is replaced, never checkout inputs.
        if (not destination.resolve().is_relative_to(build_root)
                or build_root == ROOT or build_root.is_relative_to(ROOT / "traceforge")):
            raise RuntimeError("Unsafe build resource destination")
        if destination.exists():
            shutil.rmtree(destination)
        for source in inputs:
            target = destination / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


class ResourceSdist(sdist):
    def run(self):
        resources()
        super().run()


setup(cmdclass={"build_py": ResourceBuild, "sdist": ResourceSdist})
