# Copyright 2020 The Cirq Developers
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# ========================== ISOLATED NOTEBOOK TESTS ============================================
#
# It is assumed that notebooks install cirq conditionally if they can't import cirq. This
# installation path is the main focus and it is exercised in an isolated virtual environment for
# each notebook. This is also the path that is tested in the devsite workflows, these tests meant
# to provide earlier feedback.
#
# In case the dev environment changes or this particular file changes, all notebooks are executed!
# This can take a long time and even lead to timeout on Github Actions, hence partitioning of the
# tests is possible, via setting the NOTEBOOK_PARTITIONS env var to e.g. 5, and then passing to
# pytest the `-k partition-0` or `-k partition-1`, etc. argument to limit to the given partition.

from __future__ import annotations

import os
import re
import shutil
import subprocess
import warnings

import pytest

from dev_tools import shell_tools
from dev_tools.notebooks import filter_notebooks, list_all_notebooks, REPO_ROOT, rewrite_notebook

# these notebooks rely on features that are not released yet
# after every release we should raise a PR and empty out this list
# note that these notebooks are still tested in dev_tools/notebook_test.py
# Please, always indicate in comments the feature used for easier bookkeeping.

NOTEBOOKS_DEPENDING_ON_UNRELEASED_FEATURES: list[str] = [
    # Requires `load_device_noise_properties` from #7369
    'docs/hardware/qubit_picking.ipynb',
    'docs/simulate/noisy_simulation.ipynb',
    'docs/simulate/quantum_virtual_machine.ipynb',
    'docs/simulate/qvm_basic_example.ipynb',
    # Remove once the renaming of `whitelisted_users` -> `allowlisted_users`
    # throughout cirq_google is released.
    'docs/simulate/virtual_engine_interface.ipynb',
]

# By default all notebooks should be tested, however, this list contains exceptions to the rule
# please always add a reason for skipping.
SKIP_NOTEBOOKS = [
    # skipping vendor notebooks as we don't have auth sorted out
    '**/aqt/*.ipynb',
    '**/azure-quantum/*.ipynb',
    '**/ionq/*.ipynb',
    '**/pasqal/*.ipynb',
    # skipping quantum utility simulation (too large)
    'examples/advanced/*quantum_utility*',
    # tutorials that use QCS and arent skipped due to one or more cleared output cells
    'docs/tutorials/google/identifying_hardware_changes.ipynb',
    'docs/tutorials/google/echoes.ipynb',
    # temporary: need to fix QVM metrics and device spec
    'docs/tutorials/google/spin_echoes.ipynb',
    'docs/tutorials/google/visualizing_calibration_metrics.ipynb',
]
SKIP_NOTEBOOKS += [
    # notebooks that import the examples module which is not installed with cirq
    'examples/direct_fidelity_estimation.ipynb',
    'examples/stabilizer_code.ipynb',
]
SKIP_NOTEBOOKS += NOTEBOOKS_DEPENDING_ON_UNRELEASED_FEATURES

# As these notebooks run in an isolated env, we want to minimize dependencies that are
# installed. We assume colab packages (feel free to add dependencies here that appear in colab, as
# needed by the notebooks) exist. These packages are installed into a base environment as a starting
# point, that is then cloned to a separate folder for each test.
PACKAGES = [
    # for running the notebooks
    "papermill",
    "jupyter",
    # assumed to be part of colab
    "seaborn~=0.12",
]


def _find_base_revision():
    for rev in ['upstream/main', 'origin/main', 'main']:
        try:
            result = subprocess.run(
                f'git cat-file -t {rev}'.split(), stdout=subprocess.PIPE, universal_newlines=True
            )
            if result.stdout == "commit\n":
                return rev
        except subprocess.CalledProcessError as e:
            print(e)
    raise ValueError("Can't find a base revision to compare the files with.")


def _list_changed_notebooks() -> list[str]:
    try:
        rev = _find_base_revision()
        output = subprocess.check_output(
            f'git diff --merge-base --diff-filter=d --name-only {rev}'.split(),
            cwd=REPO_ROOT,
            text=True,
        )
        changed_files = [str(REPO_ROOT.joinpath(f)) for f in output.splitlines()]
        # run all tests if this file or any of the dev tool dependencies change
        if any(
            f.endswith("isolated_notebook_test.py") or "/dev_tools/requirements/" in f
            for f in changed_files
        ):
            return list_all_notebooks()
        return [f for f in changed_files if f.endswith(".ipynb")]
    except ValueError as e:
        # It would be nicer if we could somehow automatically skip the execution of this completely,
        # however, in order to be able to rely on parallel pytest (xdist) we need parametrization to
        # work, thus this will be executed during the collection phase even when the notebook tests
        # are not included (the "-m slow" flag is not passed to pytest). So, in order to not break
        # the complete test collection phase in other tests where there is no git history
        # (fetch-depth:1), we'll just swallow the error here with a warning.
        warnings.warn(
            f"No changed notebooks are tested "
            f"(this is expected in non-notebook tests in CI): {e}"
        )
        return []


def _partitioned_test_cases(notebooks):
    n_partitions = int(os.environ.get("NOTEBOOK_PARTITIONS", "1"))
    return [(f"partition-{i%n_partitions}", notebook) for i, notebook in enumerate(notebooks)]


def _rewrite_and_run_notebook(notebook_path, cloned_env):
    notebook_file = os.path.basename(notebook_path)
    notebook_rel_dir = os.path.dirname(os.path.relpath(notebook_path, REPO_ROOT))
    out_path = f"out/{notebook_rel_dir}/{notebook_file[:-6]}.out.ipynb"
    notebook_env = cloned_env("isolated_notebook_tests", *PACKAGES)

    notebook_file = os.path.basename(notebook_path)

    rewritten_notebook_path = rewrite_notebook(notebook_path)

    REPO_ROOT.joinpath("out", notebook_rel_dir).mkdir(parents=True, exist_ok=True)
    cmd = f"""
. ./bin/activate
pip list
papermill {rewritten_notebook_path} {REPO_ROOT/out_path}"""
    result = shell_tools.run(
        cmd,
        log_run_to_stderr=False,
        shell=True,
        check=False,
        cwd=notebook_env,
        capture_output=True,
        # important to get rid of PYTHONPATH specifically, which contains
        # the Cirq repo path due to check/pytest
        env={},
    )

    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        pytest.fail(
            f"Notebook failure: {notebook_file}, please see {out_path} for the output "
            f"notebook (in Github Actions, you can download it from the workflow artifact"
            f" 'notebook-outputs'). \n"
            f"If this is a new failure in this notebook due to a new change, "
            f"that is only available in main for now, consider adding "
            f"`pip install --upgrade cirq~=1.0.dev` "
            f"instead of `pip install cirq` to this notebook, and exclude it from "
            f"dev_tools/notebooks/isolated_notebook_test.py."
        )
    os.remove(rewritten_notebook_path)
    shutil.rmtree(notebook_env)


@pytest.mark.slow
@pytest.mark.parametrize(
    "partition, notebook_path",
    _partitioned_test_cases(filter_notebooks(_list_changed_notebooks(), SKIP_NOTEBOOKS)),
)
def test_changed_notebooks_against_released_cirq(partition, notebook_path, cloned_env) -> None:
    """Tests changed notebooks in isolated virtual environments.

    In order to speed up the execution of these tests an auxiliary file may be supplied which
    performs substitutions on the notebook to make it faster.

    Specifically for a notebook file notebook.ipynb, one can supply a file notebook.tst which
    contains the substitutes.  The substitutions are provide in the form `pattern->replacement`
    where the pattern is what is matched and replaced. While the pattern is compiled as a
    regular expression, it is considered best practice to not use complicated regular expressions.
    Lines in this file that do not have `->` are ignored.
    """
    _rewrite_and_run_notebook(notebook_path, cloned_env)


@pytest.mark.weekly
@pytest.mark.parametrize(
    "partition, notebook_path",
    _partitioned_test_cases(filter_notebooks(list_all_notebooks(), SKIP_NOTEBOOKS)),
)
def test_all_notebooks_against_released_cirq(partition, notebook_path, cloned_env) -> None:
    """Tests all notebooks in isolated virtual environments.

    See `test_changed_notebooks_against_released_cirq` for more details on
    notebooks execution.
    """
    _rewrite_and_run_notebook(notebook_path, cloned_env)


@pytest.mark.parametrize("notebook_path", NOTEBOOKS_DEPENDING_ON_UNRELEASED_FEATURES)
def test_ensure_unreleased_notebooks_install_cirq_pre(notebook_path) -> None:
    # utf-8 is important for Windows testing, otherwise characters like ┌──┐ fail on cp1252
    with open(notebook_path, encoding="utf-8") as notebook:
        content = notebook.read()
        mandatory_matches = [
            r"!pip install --upgrade --quiet cirq(-google)?~=1.0.dev",
            r"Note: this notebook relies on unreleased Cirq features\. "
            r"If you want to try these features, make sure you install cirq(-google)? via "
            r"`pip install --upgrade cirq(-google)?~=1.0.dev`\.",
        ]

        for m in mandatory_matches:
            assert re.search(m, content), (
                f"{notebook_path} is marked as NOTEBOOKS_DEPENDING_ON_UNRELEASED_FEATURES, "
                f"however it contains no line matching:\n{m}"
            )


def test_skip_notebooks_has_valid_patterns() -> None:
    """Verify patterns in SKIP_NOTEBOOKS are all valid."""
    patterns_without_match = [g for g in SKIP_NOTEBOOKS if not any(REPO_ROOT.glob(g))]
    assert patterns_without_match == []
