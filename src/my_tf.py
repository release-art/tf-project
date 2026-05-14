#!/usr/bin/env python3
"""Terraform customisations."""

import argparse
import dataclasses
import json
import os
import pathlib
import subprocess
import contextlib
import typing
import tempfile

PROJ_ROOT = pathlib.Path(__file__).parent.parent.resolve()
TMP_DIR = PROJ_ROOT / "tmp"

assert TMP_DIR.is_dir()

PROJECT_STATE_PREFIX = "terraform/azure/"
MY_TF_STATE_FNAME = TMP_DIR / "my_terraform_state.json"


@dataclasses.dataclass(kw_only=True, slots=True)
class MyState:
    tfvars: str
    source_root: str
    tfplan_location: str
    environ: dict[str, str]
    backend_config: dict[str, str]

    @contextlib.contextmanager
    def decrypted_tfvars(self) -> typing.Generator[pathlib.Path, None, None]:
        original_tfvars = pathlib.Path(self.tfvars)
        assert original_tfvars.is_file(), f"tfvars file not found: {original_tfvars}"
        with tempfile.NamedTemporaryFile("w", suffix=".tfvars", delete=False) as fout:
            os.unlink(fout.name)
            subprocess.check_call(
                [
                    "op",
                    "inject",
                    "--in-file",
                    str(original_tfvars),
                    "--out-file",
                    fout.name,
                ]
            )
            yield pathlib.Path(fout.name)
            os.unlink(fout.name)


def find_project_info(tfvars):
    with tfvars.open("r") as fin:
        for line in fin.readlines():
            if line.startswith("#"):
                maybe_json = line.strip("#").strip()
                try:
                    data = json.loads(maybe_json)
                except json.decoder.JSONDecodeError:
                    continue
                if isinstance(data, dict) and data.get("header") == "terraform":
                    return data
    raise NotImplementedError("Project info comment not found")


def load_state(args):
    if MY_TF_STATE_FNAME.exists():
        with MY_TF_STATE_FNAME.open("r") as fin:
            return MyState(**json.load(fin))
    else:
        return None


def do_tf_init(args):
    tfvars = pathlib.Path(args.tfvars.name).resolve()
    project_info = find_project_info(tfvars)
    project_name = project_info.get("project")
    print(f"==== PROJECT: {project_name} ====")
    args.tfvars.close()
    tfstate_name = tfvars.stem
    old_state = load_state(args)
    init_env = {}
    if old_state:
        init_env.update(old_state.environ)
    state = MyState(
        tfvars=str(tfvars),
        source_root=str(PROJ_ROOT / "terraform" / project_name),
        tfplan_location=str(TMP_DIR / "my.tfplan"),
        environ=init_env,
        backend_config={"key": f"{PROJECT_STATE_PREFIX}{tfvars.stem}.tfstate"},
    )
    cmd = [
        "terraform",
        f"-chdir={state.source_root}",
        "init",
        "-upgrade",
        "-reconfigure",
    ]
    for key, value in state.backend_config.items():
        cmd.extend(["-backend-config", f"{key}={value}"])
    subprocess.check_call(cmd)
    with MY_TF_STATE_FNAME.open("w") as fout:
        json.dump(dataclasses.asdict(state), fout, indent=4, sort_keys=True)


def get_tf_environ(state):
    out = os.environ.copy()
    out.update(state.environ)
    return out


def _target_args(args) -> list[str]:
    return [f"-target={t}" for t in (args.targets or [])]


def _replace_args(args) -> list[str]:
    return [f"-replace={r}" for r in (getattr(args, "replaces", None) or [])]


def do_tf_plan(args):
    state = load_state(args)
    with state.decrypted_tfvars() as decrypted_tfvars:
        subprocess.check_call(
            [
                "terraform",
                f"-chdir={state.source_root}",
                "plan",
                f"-var-file={decrypted_tfvars}",
                *_target_args(args),
                *_replace_args(args),
                f"-out={state.tfplan_location}",
            ],
            env=get_tf_environ(state),
        )


def do_tf_apply(args):
    state = load_state(args)
    with state.decrypted_tfvars() as decrypted_tfvars:
        subprocess.check_call(
            [
                "terraform",
                f"-chdir={state.source_root}",
                "apply",
                f"-var-file={decrypted_tfvars}",
                state.tfplan_location,
            ],
            env=get_tf_environ(state),
        )
    pathlib.Path(state.tfplan_location).unlink()


def do_tf_refresh(args):
    state = load_state(args)
    with state.decrypted_tfvars() as decrypted_tfvars:
        subprocess.check_call(
            [
                "terraform",
                f"-chdir={state.source_root}",
                "apply",
                f"-var-file={decrypted_tfvars}",
                *_target_args(args),
            ],
            env=get_tf_environ(state),
        )


def do_tf_destroy(args):
    state = load_state(args)
    with state.decrypted_tfvars() as decrypted_tfvars:
        subprocess.check_call(
            [
                "terraform",
                f"-chdir={state.source_root}",
                "destroy",
                f"-var-file={decrypted_tfvars}",
                *_target_args(args),
            ],
            env=get_tf_environ(state),
        )


def do_tf_fmt(args):
    subprocess.check_call(
        [
            "terraform",
            "fmt",
            "-recursive",
            PROJ_ROOT / "terraform",
            PROJ_ROOT / "tfvars",
        ]
    )

def do_tf_output(args):
    state = load_state(args)
    subprocess.check_call(
        [
            "terraform",
            f"-chdir={state.source_root}",
            "output",
            "-json",
            # f"-state={MY_TF_STATE_FNAME}",
        ]
    )


def do_tf_state_mv(args):
    state = load_state(args)
    subprocess.check_call(
        [
            "terraform",
            f"-chdir={state.source_root}",
            "state",
            "mv",
            args.source,
            args.destination,
        ],
        env=get_tf_environ(state),
    )

def get_argument_parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(required=True)
    init_p = subparsers.add_parser("init", help="tfinit")
    init_p.add_argument("tfvars", help="Target tfvars", type=argparse.FileType("r"))
    init_p.set_defaults(__main__=do_tf_init)

    plan_p = subparsers.add_parser("plan")
    plan_p.add_argument(
        "--target", "-t",
        dest="targets",
        action="append",
        default=[],
        metavar="RESOURCE",
        help="Limit planning to a specific resource address (repeatable).",
    )
    plan_p.add_argument(
        "--replace", "-r",
        dest="replaces",
        action="append",
        default=[],
        metavar="RESOURCE",
        help="Force replacement of a specific resource address (repeatable).",
    )
    plan_p.set_defaults(__main__=do_tf_plan)

    apply_p = subparsers.add_parser("apply")
    apply_p.set_defaults(__main__=do_tf_apply)

    refresh_p = subparsers.add_parser("refresh")
    refresh_p.add_argument(
        "--target", "-t",
        dest="targets",
        action="append",
        default=[],
        metavar="RESOURCE",
        help="Limit apply to a specific resource address (repeatable).",
    )
    refresh_p.set_defaults(__main__=do_tf_refresh)

    destroy_p = subparsers.add_parser("destroy")
    destroy_p.add_argument(
        "--target", "-t",
        dest="targets",
        action="append",
        default=[],
        metavar="RESOURCE",
        help="Limit destruction to a specific resource address (repeatable).",
    )
    destroy_p.set_defaults(__main__=do_tf_destroy)

    apply_p = subparsers.add_parser("fmt")
    apply_p.set_defaults(__main__=do_tf_fmt)

    output_p = subparsers.add_parser("output")
    output_p.set_defaults(__main__=do_tf_output)

    state_mv_p = subparsers.add_parser("state-mv", help="Move a resource in Terraform state")
    state_mv_p.add_argument("source", help="Source resource address")
    state_mv_p.add_argument("destination", help="Destination resource address")
    state_mv_p.set_defaults(__main__=do_tf_state_mv)

    return parser


if __name__ == "__main__":
    args = get_argument_parser().parse_args()
    args.__main__(args)
