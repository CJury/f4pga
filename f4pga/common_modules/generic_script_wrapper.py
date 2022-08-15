#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2022 F4PGA Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""
This module is intended for wrapping simple scripts without rewriting them as
an sfbuild module. This is mostly to maintain compatibility with workflows
that do not use sfbuild and instead rely on legacy scripts.

Accepted module parameters:
* `stage_name` (string, optional): Name describing the stage
* `script` (string, mandatory): Path to the script to be executed
* `interpreter` (string, optional): Interpreter for the script
* `cwd` (string, optional): Current Working Directory for the script
* `outputs` (dict[string -> dict[string -> string]], mandatory):
  A dict with output descriptions (dicts).
  Keys name output dependencies.
  * `mode` (string, mandatory): "file" or "stdout".
    Describes how the output is grabbed from the script.
  * `file` (string, required if `mode` is "file"): Name of the file generated by the script.
  * `target` (string, required): Default name of the file of the generated dependency.
    You can use all values available durng map_io stage.
    Each input dependency alsogets two extra values associated with it:
    `:dependency_name[noext]`, which contains the path to the dependency the extension with anything after last "."
    removed and `:dependency_name[dir]` which contains directory paths of the dependency.
    This is useful for deriving an output name from the input.
  * `meta` (string, optional): Description of the output dependency.
* `inputs` (dict[string -> string | bool], mandatory):
  A dict with input descriptions.
  Key is either a name of a named argument or a position of unnamed argument prefaced with "#" (eg. "#1").
  Positions are indexed from 1, as it's a convention that 0th argument is the path of the executed program.
  Values are strings that can contains references to variables to be resolved after the project flow configuration is
  loaded (that means they can reference values and dependencies which are to be set by the user).
  All of modules inputs will be determined by the references used.
  Thus dependency and value definitions are implicit.
  If the value of the resolved string is empty and is associated with a named argument, the argument in question will be
  skipped entirely.
  This allows using optional dependencies.
  To use a named argument as a flag instead, set it to `true`.
"""

# TODO: `environment` input kind

from pathlib import Path
from re import match as re_match, finditer as re_finditer

from f4pga.common import decompose_depname, deep, get_verbosity_level, sub
from f4pga.module import Module, ModuleContext


def _get_param(params, name: str):
    param = params.get(name)
    if not param:
        raise Exception(f'generic module wrapper parameters '
                        f'missing `{name}` field')
    return param


def _parse_param_def(param_def: str):
    if param_def[0] == '#':
        return 'positional', int(param_def[1:])
    elif param_def[0] == '$':
        return 'environmental', param_def[1:]
    return 'named', param_def


class InputReferences:
    dependencies: 'set[str]'
    values: 'set[str]'

    def merge(self, other):
        self.dependencies.update(other.dependencies)
        self.values.update(other.values)

    def __init__(self):
        self.dependencies = set()
        self.values = set()


def _get_input_references(input: str) -> InputReferences:
    refs = InputReferences()
    if type(input) is not str:
        return refs
    for match in re_finditer('\$\{([^${}]*)\}', input):
        match_str = match.group(1)
        if match_str[0] != ':':
            refs.values.add(match_str)
            continue
        if len(match_str) < 2:
            raise Exception('Dependency name must be at least 1 character long')
        refs.dependencies.add(re_match('([^\\[\\]]*)', match_str[1:]).group(1))
    return refs


def _make_noop1():
    def noop(_):
        return
    return noop


def _tailcall1(self, fun):
    def newself(arg, self=self, fun=fun):
        fun(arg)
        self(arg)
    return newself


class GenericScriptWrapperModule(Module):
    script_path: str
    stdout_target: 'None | tuple[str, str]'
    file_outputs: 'list[tuple[str, str, str]]'
    interpreter: 'None | str'
    cwd: 'None | str'

    @staticmethod
    def _add_extra_values_to_env(ctx: ModuleContext):
        for take_name, take_path in vars(ctx.takes).items():
            if take_path is not None:
                ctx.r_env.values[f':{take_name}[noext]'] = deep(lambda p: str(Path(p).with_suffix('')))(take_path)
                ctx.r_env.values[f':{take_name}[dir]'] = deep(lambda p: str(Path(p).parent.resolve()))(take_path)

    def map_io(self, ctx: ModuleContext):
        self._add_extra_values_to_env(ctx)

        outputs = {}
        for dep, _, out_path in self.file_outputs:
            out_path_resolved = ctx.r_env.resolve(out_path, final=True)
            outputs[dep] = out_path_resolved

        if self.stdout_target:
            out_path_resolved = \
                ctx.r_env.resolve(self.stdout_target[1], final=True)
            outputs[self.stdout_target[0]] = out_path_resolved

        return outputs

    def execute(self, ctx: ModuleContext):
        self._add_extra_values_to_env(ctx)

        cwd = ctx.r_env.resolve(self.cwd)

        sub_args = [ctx.r_env.resolve(self.script_path, final=True)] \
            + self.get_args(ctx)
        if self.interpreter:
            sub_args = [ctx.r_env.resolve(self.interpreter, final=True)] + sub_args

        sub_env = self.get_env(ctx)

        # XXX: This may produce incorrect string if arguments contains whitespace
        #      characters
        cmd = ' '.join(sub_args)

        if get_verbosity_level() >= 2:
            yield f'Running script...\n           {cmd}'
        else:
            yield f'Running an externel script...'

        data = sub(*sub_args, cwd=cwd, env=sub_env)

        yield 'Writing outputs...'
        if self.stdout_target:
            target = ctx.r_env.resolve(self.stdout_target[1], final=True)
            with open(target, 'wb') as f:
                f.write(data)

        for _, file, target in self.file_outputs:
            file = ctx.r_env.resolve(file, final=True)
            target = ctx.r_env.resolve(target, final=True)
            if target != file:
                Path(file).rename(target)

    def _init_outputs(self, output_defs: 'dict[str, dict[str, str]]'):
        self.stdout_target = None
        self.file_outputs = []

        for dep_name, output_def in output_defs.items():
            dname, _ = decompose_depname(dep_name)
            self.produces.append(dep_name)
            meta = output_def.get('meta')
            if meta is str:
                self.prod_meta[dname] = meta

            mode = output_def.get('mode')
            if type(mode) is not str:
                raise Exception(f'Output mode for `{dep_name}` is not specified')

            target = output_def.get('target')
            if type(target) is not str:
                raise Exception('`target` field is not specified')

            if mode == 'file':
                file = output_def.get('file')
                if type(file) is not str:
                    raise Exception('Output file is not specified')
                self.file_outputs.append((dname, file, target))
            elif mode == 'stdout':
                if self.stdout_target is not None:
                    raise Exception('stdout output is already specified')
                self.stdout_target = dname, target

    # A very functional approach
    def _init_inputs(self, input_defs):
        positional_args = []
        named_args = []
        env_vars = {}
        refs = InputReferences()

        get_args = _make_noop1()
        get_env = _make_noop1()

        for arg_code, input in input_defs.items():
            param_kind, param = _parse_param_def(arg_code)

            push = None
            push_env = None
            if param_kind == 'named':
                def push_named(val: 'str | bool | int', param=param):
                    nonlocal named_args
                    if type(val) is bool:
                        named_args.append(f'--{param}')
                    else:
                        named_args += [f'--{param}', str(val)]
                push = push_named
            elif param_kind == 'environmental':
                def push_environ(val: 'str | bool | int', param=param):
                    nonlocal env_vars
                    env_vars[param] = val
                push_env = push_environ
            else:
                def push_positional(val: str, param=param):
                    nonlocal positional_args
                    positional_args.append((param, val))
                push = push_positional

            input_refs = _get_input_references(input)
            refs.merge(input_refs)

            if push is not None:
                def push_q(ctx: ModuleContext, push=push, input=input):
                    val = ctx.r_env.resolve(input, final=True)
                    if val != '':
                        push(val)
                get_args = _tailcall1(get_args, push_q)
            else:
                def push_q(ctx: ModuleContext, push_env=push_env, input=input):
                    val = ctx.r_env.resolve(input, final=True)
                    if val != '':
                        push_env(val)
                get_env = _tailcall1(get_env, push_q)

        def get_all_args(ctx: ModuleContext):
            nonlocal get_args, positional_args, named_args

            get_args(ctx)

            positional_args.sort(key=lambda t: t[0])
            pos =  [ a for _, a in positional_args]

            return named_args + pos

        def get_all_env(ctx: ModuleContext):
            nonlocal get_env, env_vars
            get_env(ctx)
            if len(env_vars.items()) == 0:
                return None
            return env_vars

        setattr(self, 'get_args', get_all_args)
        setattr(self, 'get_env', get_all_env)

        for dep in refs.dependencies:
            self.takes.append(dep)
        for val in refs.values:
            self.values.append(val)

    def __init__(self, params):
        stage_name = params.get('stage_name')
        self.name = f"{'<unknown>' if stage_name is None else stage_name}-generic"
        self.no_of_phases = 2
        self.script_path = params.get('script')
        self.interpreter = params.get('interpreter')
        self.cwd = params.get('cwd')
        self.takes = []
        self.produces = []
        self.values = []
        self.prod_meta = {}

        self._init_outputs(_get_param(params, 'outputs'))
        self._init_inputs(_get_param(params, 'inputs'))

ModuleClass = GenericScriptWrapperModule
