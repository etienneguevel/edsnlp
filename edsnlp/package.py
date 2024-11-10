import os
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Mapping,
    Optional,
    Sequence,
    Union,
)

import build
import confit
import setuptools
import toml
from build.__main__ import build_package, build_package_via_sdist
from confit import Cli
from loguru import logger
from typing_extensions import Literal, TypedDict

import edsnlp
from edsnlp.utils.typing import AsList, Validated

PoetryConstraint = TypedDict(
    "PoetryConstraint",
    {
        "version": str,
        "extras": Optional[Sequence[str]],
        "markers": Optional[str],
        "url": Optional[str],
        "path": Optional[str],
        "git": Optional[str],
        "ref": Optional[str],
        "branch": Optional[str],
        "tag": Optional[str],
    },
    total=False,
)

logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> - <level>{level: <8}</level>"
    " - {message}",
)
py_version = f"{sys.version_info.major}.{sys.version_info.minor}"

app = Cli(pretty_exceptions_show_locals=False, pretty_exceptions_enable=False)


def snake_case(s):
    # From https://www.w3resource.com/python-exercises/string/python-data-type-string-exercise-97.php  # noqa E501
    return "_".join(
        re.sub(
            "([A-Z][a-z]+)", r" \1", re.sub("([A-Z]+)", r" \1", s.replace("-", " "))
        ).split()
    ).lower()


class ModuleName(str, Validated):
    def __new__(cls, *args, **kwargs):
        raise NotImplementedError("ModuleName is only meant for typing.")

    @classmethod
    def validate(cls, value, config=None):
        if not isinstance(value, str):
            raise TypeError("string required")

        if not re.match(
            r"^([A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9])$", value, flags=re.IGNORECASE
        ):
            raise ValueError("invalid identifier")
        return value


if TYPE_CHECKING:
    ModuleName = str  # noqa F811

POETRY_SNIPPET = """\
from poetry.core.masonry.builders.sdist import SdistBuilder
from poetry.factory import Factory
from poetry.core.masonry.utils.module import ModuleOrPackageNotFound
import sys
# Initialize the Poetry object for the current project
poetry = Factory().create_poetry("__root_dir__")

# Initialize the builder
try:
    builder = SdistBuilder(poetry, None, None)
    # Get the list of files to include
    files = builder.find_files_to_add()
except ModuleOrPackageNotFound:
    if not poetry.package.packages:
        print([])
        sys.exit(0)

print([
    {k: v for k, v in {
    "include": getattr(include, '_include'),
    "from": getattr(include, 'source', None),
    "formats": getattr(include, 'formats', None),
    }.items() if v}
    for include in builder._module.includes
])


# Print the list of files
for file in files:
    print(file.path)
"""

INIT_PY = """
# -----------------------------------------
# This section was autogenerated by edsnlp
# -----------------------------------------

import edsnlp
from pathlib import Path
from typing import Optional, Dict, Any

__version__ = {__version__}

def load(
    overrides: Optional[Dict[str, Any]] = None,
) -> edsnlp.Pipeline:
    path_outside = Path(__file__).parent / "{artifacts_dir}"
    path_inside = Path(__file__).parent / "{artifacts_dir_inside}"
    path = path_inside if path_inside.exists() else path_outside
    model = edsnlp.load(path, overrides=overrides)
    return model
"""

AUTHOR_REGEX = re.compile(r"(?P<name>.*) <(?P<email>.*)>")


def parse_authors(authors):
    authors = [authors] if isinstance(authors, str) else authors
    return [
        author
        if not isinstance(author, str)
        else dict(AUTHOR_REGEX.match(author).groupdict())
        for author in authors
    ]


def replace_with_dict(content: str, replacements: dict):
    # Replace HTML elements with the corresponding values from the dictionary
    for key, replacement in replacements.items():
        # Create a regex pattern to find HTML elements with the given id
        content = re.sub(key, replacement, content, flags=re.DOTALL)
    return content


class Packager:
    def __init__(
        self,
        *,
        name: ModuleName,
        pyproject: Optional[Dict[str, Any]],
        pipeline: Union[Path, "edsnlp.Pipeline"],
        version: Optional[str],
        root_dir: Path = ".",
        build_dir: Optional[Path] = None,
        dist_dir: Path,
        artifacts_name: ModuleName,
        exclude: AsList[str],
        readme_replacements: Dict[str, str] = {},
        file_paths: Sequence[Path],
    ):
        self.name = name
        self.version = version
        assert name == pyproject["project"]["name"]
        assert version == pyproject["project"]["version"]
        self.root_dir = root_dir.resolve()
        self.pipeline = pipeline
        self.artifacts_name = artifacts_name
        self.dist_dir = (
            dist_dir if Path(dist_dir).is_absolute() else self.root_dir / dist_dir
        )
        self.build_dir = build_dir
        self.readme_replacements = readme_replacements
        self.exclude = exclude
        self.file_paths = file_paths
        self.pyproject = pyproject

        logger.info(f"root_dir: {root_dir}")
        logger.info(f"artifacts_name: {artifacts_name}")
        logger.info(f"name: {name}")

    def build(
        self,
        distributions: Sequence[str] = (),
        config_settings: Optional[build.ConfigSettingsType] = None,
        isolation: bool = True,
        skip_dependency_check: bool = False,
    ):
        logger.info("Building package")

        if distributions:
            build_call = build_package
        else:
            build_call = build_package_via_sdist
            distributions = ["wheel"]
        build_call(  # type: ignore
            srcdir=self.build_dir,
            outdir=self.dist_dir,
            distributions=distributions,
            config_settings=config_settings,
            isolation=isolation,
            skip_dependency_check=skip_dependency_check,
        )

    # def update_pyproject(self):
    #     # Adding artifacts to include in pyproject.toml
    #     snake_name = snake_case(self.name.lower())
    #     included = self.pyproject["tool"]["poetry"].setdefault("include", [])
    #     included.append(f"{snake_name}/{self.artifacts_name}/**")
    #     packages = list(self.packages)
    #     packages.append({"include": snake_name})
    #     self.pyproject["tool"]["poetry"]["packages"] = packages

    def make_src_dir(self):
        snake_name = snake_case(self.name.lower())
        package_dir = self.build_dir / snake_name

        shutil.rmtree(package_dir, ignore_errors=True)
        os.makedirs(package_dir, exist_ok=True)
        build_artifacts_dir = self.build_dir / self.artifacts_name
        for file_path in self.file_paths:
            dest_path = self.build_dir / Path(file_path).relative_to(self.root_dir)
            if isinstance(self.pipeline, Path) and self.pipeline in file_path.parents:
                raise Exception(
                    f"Pipeline ({self.artifacts_name}) is already "
                    "included in the package's data, you should "
                    "remove it from the pyproject.toml metadata."
                )
            os.makedirs(dest_path.parent, exist_ok=True)
            shutil.copy(file_path, dest_path)

        # self.update_pyproject()

        # Write pyproject.toml
        (self.build_dir / "pyproject.toml").write_text(toml.dumps(self.pyproject))
        if "readme" in self.pyproject["project"]:
            readme = (self.root_dir / self.pyproject["project"]["readme"]).read_text()
            readme = replace_with_dict(readme, self.readme_replacements)
            (self.build_dir / "README.md").write_text(readme)

        if isinstance(self.pipeline, Path):
            # self.pipeline = edsnlp.load(self.pipeline)
            shutil.copytree(
                self.pipeline,
                build_artifacts_dir,
            )
        else:
            self.pipeline.to_disk(build_artifacts_dir, exclude=set())

        # After building wheel, artifacts will be placed inside the
        # package dir, not next to it as in source distribution so
        # we let the load script test both locations
        with open(package_dir / "__init__.py", mode="a") as f:
            f.write(
                INIT_PY.format(
                    __version__=repr(self.version),
                    artifacts_dir=os.path.relpath(build_artifacts_dir, package_dir),
                    artifacts_dir_inside=self.artifacts_name,
                )
            )

        # Print all the files that will be included in the package
        for file in self.build_dir.rglob("*"):
            if file.is_file():
                rel = file.relative_to(self.build_dir)
                if not any(rel.match(e) for e in self.exclude):
                    logger.info(f"INCLUDE {rel}")
                else:
                    file.unlink()
                    logger.info(f"SKIP {rel}")


class PoetryPackager(Packager):
    def __init__(
        self,
        *,
        name: ModuleName,
        pyproject: Optional[Dict[str, Any]],
        pipeline: Union[Path, "edsnlp.Pipeline"],
        version: Optional[str],
        root_dir: Path = ".",
        build_dir: Optional[Path] = None,
        dist_dir: Path,
        artifacts_name: ModuleName,
        metadata: Optional[Dict[str, Any]] = {},
        exclude: AsList[str],
        readme_replacements: Dict[str, str] = {},
    ):
        try:
            version = version or pyproject["tool"]["poetry"]["version"]
        except (KeyError, TypeError):  # pragma: no cover
            version = "0.1.0"
        name = name or pyproject["tool"]["poetry"]["name"]
        main_package = (
            snake_case(pyproject["tool"]["poetry"]["name"].lower())
            if pyproject is not None
            else None
        )
        model_package = snake_case(name.lower())

        root_dir = root_dir.resolve()
        dist_dir = dist_dir if Path(dist_dir).is_absolute() else root_dir / dist_dir

        build_dir = Path(tempfile.mkdtemp()) if build_dir is None else build_dir

        new_pyproject: Dict[str, Any] = {
            "build-system": {
                "requires": ["hatchling"],
                "build-backend": "hatchling.build",
            },
            "tool": {"hatch": {"build": {}}},
            "project": {
                "name": model_package,
                "version": version,
                "requires-python": ">=3.7",
            },
        }
        file_paths = []

        if pyproject is not None:
            poetry = pyproject["tool"]["poetry"]

            # Extract packages
            poetry_bin_path = (
                subprocess.run(["which", "poetry"], stdout=subprocess.PIPE)
                .stdout.decode()
                .strip()
            )
            python_executable = Path(poetry_bin_path).read_text().split("\n")[0][2:]
            result = subprocess.run(
                [
                    *python_executable.split(),
                    "-c",
                    POETRY_SNIPPET.replace("__root_dir__", str(root_dir)),
                ],
                stdout=subprocess.PIPE,
                cwd=root_dir,
            )
            if result.returncode != 0:
                raise Exception()
            out = result.stdout.decode().strip().split("\n")
            file_paths = [root_dir / file_path for file_path in out[1:]]
            packages = {
                main_package,
                model_package,
                *(package["include"] for package in eval(out[0])),
            }
            packages = sorted([p for p in packages if p])
            new_pyproject["tool"]["hatch"]["build"] = {
                "packages": [*packages, artifacts_name],
                "exclude": ["__pycache__/", "*.pyc", "*.pyo", ".ipynb_checkpoints"],
                "artifacts": [artifacts_name],
                "targets": {
                    "wheel": {
                        "sources": {
                            f"{artifacts_name}": f"{model_package}/{artifacts_name}"
                        },
                    },
                },
            }
            if "description" in poetry:  # pragma: no cover
                new_pyproject["project"]["description"] = poetry["description"]
            if "classifiers" in poetry:  # pragma: no cover
                new_pyproject["project"]["classifiers"] = poetry["classifiers"]
            if "keywords" in poetry:  # pragma: no cover
                new_pyproject["project"]["keywords"] = poetry["keywords"]
            if "license" in poetry:  # pragma: no cover
                new_pyproject["project"]["license"] = {"text": poetry["license"]}
            if "readme" in poetry:  # pragma: no cover
                new_pyproject["project"]["readme"] = poetry["readme"]
            if "authors" in poetry:  # pragma: no cover
                new_pyproject["project"]["authors"] = parse_authors(poetry["authors"])
            if "plugins" in poetry:  # pragma: no cover
                new_pyproject["project"]["entry-points"] = poetry["plugins"]
            if "scripts" in poetry:  # pragma: no cover
                new_pyproject["project"]["scripts"] = poetry["scripts"]

            # Dependencies
            deps = []
            poetry_deps = poetry["dependencies"]
            for dep_name, constraint in poetry_deps.items():
                dep = dep_name
                constraint: PoetryConstraint = (
                    dict(constraint)
                    if isinstance(constraint, dict)
                    else {"version": constraint}
                )
                try:
                    dep += f"[{','.join(constraint.pop('extras'))}]"
                except KeyError:
                    pass
                if "version" in constraint:
                    dep_version = constraint.pop("version")
                    assert not dep_version.startswith(
                        "^"
                    ), "Packaging models with ^ dependencies is not supported"
                    dep += (
                        ""
                        if dep_version == "*"
                        else dep_version
                        if not dep_version[0].isdigit()
                        else f"=={dep_version}"
                    )
                try:
                    dep += f"; {constraint.pop('markers')}"
                except KeyError:
                    pass
                assert (
                    not constraint
                ), f"Unsupported constraints for dependency {dep_name}: {constraint}"
                if dep_name == "python":
                    new_pyproject["project"]["requires-python"] = dep.replace(
                        "python", ""
                    )
                    continue
                deps.append(dep)

            new_pyproject["project"]["dependencies"] = deps

        if "authors" in metadata:
            metadata["authors"] = parse_authors(metadata["authors"])
        metadata["name"] = model_package
        metadata["version"] = version

        new_pyproject = confit.Config(new_pyproject).merge({"project": metadata})

        # Use hatch
        super().__init__(
            name=model_package,
            pyproject=new_pyproject,
            pipeline=pipeline,
            version=version,
            root_dir=root_dir,
            build_dir=build_dir,
            dist_dir=dist_dir,
            artifacts_name=artifacts_name,
            exclude=exclude,
            readme_replacements=readme_replacements,
            file_paths=file_paths,
        )


class SetuptoolsPackager(Packager):
    def __init__(
        self,
        *,
        name: ModuleName,
        pyproject: Optional[Dict[str, Any]],
        pipeline: Union[Path, "edsnlp.Pipeline"],
        version: Optional[str],
        root_dir: Path = ".",
        build_dir: Optional[Path] = None,
        dist_dir: Path,
        artifacts_name: ModuleName,
        metadata: Optional[Dict[str, Any]] = {},
        exclude: AsList[str],
        readme_replacements: Dict[str, str] = {},
    ):
        try:
            version = version or pyproject["project"]["version"]
        except (KeyError, TypeError):
            version = "0.1.0"
        name = name or pyproject["project"]["name"]
        if pyproject is not None:
            main_package = snake_case(pyproject["project"]["name"].lower())
        else:
            main_package = None
        model_package = snake_case(name.lower())

        root_dir = root_dir.resolve()
        dist_dir = dist_dir if Path(dist_dir).is_absolute() else root_dir / dist_dir

        build_dir = Path(tempfile.mkdtemp()) if build_dir is None else build_dir

        new_pyproject: confit.Config = confit.Config()
        if pyproject is not None:
            new_pyproject["project"] = pyproject["project"]
        new_pyproject = new_pyproject.merge(
            {
                "build-system": {
                    "requires": ["hatchling"],
                    "build-backend": "hatchling.build",
                },
                "tool": {"hatch": {"build": {}}},
                "project": {
                    "name": model_package,
                    "version": version,
                    "requires-python": ">=3.7",
                },
            }
        )

        try:
            find = dict(pyproject["tool"].pop("setuptools", {})["packages"]["find"])
        except Exception:
            find = {}
        where = find.pop("where", ["."])
        where = [where] if not isinstance(where, list) else where
        packages = {main_package, model_package}
        for w in where:
            # TODO Should we handle namespaces ?
            # if find.pop("namespace", None) is not None:
            #     packages.extend(setuptools.find_namespace_packages(**find))
            packages.update(setuptools.find_packages(w, **find))
        packages = sorted([p for p in packages if p])
        file_paths = []
        for package in packages:
            file_paths.extend((root_dir / package).rglob("*"))

        new_pyproject["tool"]["hatch"]["build"] = {
            "packages": [*packages, artifacts_name],
            "exclude": ["__pycache__/", "*.pyc", "*.pyo", ".ipynb_checkpoints"],
            "artifacts": [artifacts_name],
            "targets": {
                "wheel": {
                    "sources": {
                        f"{artifacts_name}": f"{model_package}/{artifacts_name}"
                    },
                },
            },
        }

        if "authors" in metadata:
            metadata["authors"] = parse_authors(metadata["authors"])
        metadata["name"] = model_package
        metadata["version"] = version

        new_pyproject = new_pyproject.merge({"project": metadata})

        super().__init__(
            name=model_package,
            pyproject=new_pyproject,
            pipeline=pipeline,
            version=version,
            root_dir=root_dir,
            build_dir=build_dir,
            dist_dir=dist_dir,
            artifacts_name=artifacts_name,
            exclude=exclude,
            readme_replacements=readme_replacements,
            file_paths=file_paths,
        )


@app.command(name="package")
def package(
    pipeline: Union[Path, "edsnlp.Pipeline"],
    *,
    name: Optional[ModuleName] = None,
    root_dir: Path = Path("."),
    build_dir: Optional[Path] = None,
    dist_dir: Path = Path("dist"),
    artifacts_name: ModuleName = "artifacts",
    check_dependencies: bool = False,
    project_type: Optional[Literal["poetry", "setuptools"]] = None,
    version: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = {},
    distributions: Optional[AsList[Literal["wheel", "sdist"]]] = ["wheel"],
    config_settings: Optional[Mapping[str, Union[str, Sequence[str]]]] = None,
    isolation: bool = True,
    skip_build_dependency_check: bool = False,
    exclude: Optional[AsList[str]] = None,
    readme_replacements: Dict[str, str] = {},
):
    # root_dir = Path(".").resolve()
    exclude = exclude or ["artifacts/vocab/*"]
    pyproject_path = root_dir / "pyproject.toml"

    if not pyproject_path.exists():
        check_dependencies = True
        if name is None:
            raise ValueError(
                f"No pyproject.toml could be found in the root directory {root_dir}, "
                f"you need to create one, or fill the name parameter."
            )

    if check_dependencies:
        warnings.warn("check_dependencies is deprecated", DeprecationWarning)

    root_dir = root_dir.resolve()

    pyproject = None
    if pyproject_path.exists():
        pyproject = toml.loads((root_dir / "pyproject.toml").read_text())

    package_managers = {"setuptools", "poetry", "hatch", "pdm"} & set(
        (pyproject or {}).get("tool", {})
    )
    package_managers = package_managers or {"setuptools"}  # default
    try:
        if project_type is None:
            [project_type] = package_managers
        packager_cls = {
            "poetry": PoetryPackager,
            "setuptools": SetuptoolsPackager,
        }[project_type]
    except Exception:  # pragma: no cover
        raise ValueError(
            "Could not infer project type, only poetry and setuptools based projects "
            "are supported for now"
        )
    packager = packager_cls(
        pyproject=pyproject,
        pipeline=pipeline,
        name=name,
        version=version,
        root_dir=root_dir,
        build_dir=build_dir,
        dist_dir=dist_dir,
        artifacts_name=artifacts_name,
        metadata=metadata,
        exclude=exclude,
        readme_replacements=readme_replacements,
    )
    packager.make_src_dir()
    packager.build(
        distributions=distributions,
        config_settings=config_settings,
        isolation=isolation,
        skip_dependency_check=skip_build_dependency_check,
    )


if __name__ == "__main__":
    app()
