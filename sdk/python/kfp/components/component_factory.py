# Copyright 2021-2022 The Kubeflow Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import ast
import dataclasses
import inspect
import itertools
import pathlib
import re
import sys
import textwrap
from typing import Any, Callable, Dict, List, Optional, Tuple
import warnings

import docstring_parser
from kfp.components import python_component
from kfp.components import structures
from kfp.components.types import artifact_types
from kfp.components.types import type_annotations
from kfp.components.types import type_utils

_DEFAULT_BASE_IMAGE = 'python:3.7'
PYTHON_VERSION = str(sys.version_info.major) + '.' + str(sys.version_info.minor)


@dataclasses.dataclass
class ComponentInfo():
    """A dataclass capturing registered components.

    This will likely be subsumed/augmented with BaseComponent.
    """
    name: str
    function_name: str
    func: Callable
    target_image: str
    module_path: pathlib.Path
    component_spec: structures.ComponentSpec
    output_component_file: Optional[str] = None
    base_image: str = _DEFAULT_BASE_IMAGE


# A map from function_name to components.  This is always populated when a
# module containing KFP components is loaded. Primarily used by KFP CLI
# component builder to package components in a file into containers.
REGISTERED_MODULES = None


def _python_function_name_to_component_name(name):
    name_with_spaces = re.sub(' +', ' ', name.replace('_', ' ')).strip(' ')
    return name_with_spaces[0].upper() + name_with_spaces[1:]


def _make_index_url_options(pip_index_urls: Optional[List[str]]) -> str:
    if not pip_index_urls:
        return ''

    index_url = pip_index_urls[0]
    extra_index_urls = pip_index_urls[1:]

    options = [f'--index-url {index_url} --trusted-host {index_url} ']
    options.extend(
        f'--extra-index-url {extra_index_url} --trusted-host {extra_index_url} '
        for extra_index_url in extra_index_urls)

    return ' '.join(options)


_install_python_packages_script_template = '''
if ! [ -x "$(command -v pip)" ]; then
    python3 -m ensurepip || python3 -m ensurepip --user || apt-get install python3-pip
fi

PIP_DISABLE_PIP_VERSION_CHECK=1 python3 -m pip install --quiet \
    --no-warn-script-location {index_url_options}{concat_package_list} && "$0" "$@"
'''


def _get_packages_to_install_command(
        package_list: Optional[List[str]] = None,
        pip_index_urls: Optional[List[str]] = None) -> List[str]:

    if not package_list:
        return []

    concat_package_list = ' '.join(
        [repr(str(package)) for package in package_list])
    index_url_options = _make_index_url_options(pip_index_urls)
    install_python_packages_script = _install_python_packages_script_template.format(
        index_url_options=index_url_options,
        concat_package_list=concat_package_list)
    return ['sh', '-c', install_python_packages_script]


def _get_default_kfp_package_path() -> str:
    import kfp
    return 'kfp=={}'.format(kfp.__version__)


def _get_function_source_definition(func: Callable) -> str:
    func_code = inspect.getsource(func)

    # Function might be defined in some indented scope (e.g. in another
    # function). We need to handle this and properly dedent the function source
    # code
    func_code = textwrap.dedent(func_code)
    func_code_lines = func_code.split('\n')

    # Removing possible decorators (can be multiline) until the function
    # definition is found
    func_code_lines = itertools.dropwhile(lambda x: not x.startswith('def'),
                                          func_code_lines)

    if not func_code_lines:
        raise ValueError(
            'Failed to dedent and clean up the source of function "{}". '
            'It is probably not properly indented.'.format(func.__name__))

    return '\n'.join(func_code_lines)


def _annotation_to_type_struct(annotation):
    if not annotation or annotation == inspect.Parameter.empty:
        return None
    if hasattr(annotation, 'to_dict'):
        annotation = annotation.to_dict()
    if isinstance(annotation, dict):
        return annotation
    if isinstance(annotation, type):
        type_struct = type_utils.get_canonical_type_name_for_type(annotation)
        if type_struct:
            return type_struct

        if issubclass(annotation, artifact_types.Artifact
                     ) and not annotation.TYPE_NAME.startswith('system.'):
            # For artifact classes not under the `system` namespace,
            # use its TYPE_NAME as-is.
            type_name = annotation.TYPE_NAME
        else:
            type_name = str(annotation.__name__)

    elif hasattr(annotation,
                 '__forward_arg__'):  # Handling typing.ForwardRef('Type_name')
        type_name = str(annotation.__forward_arg__)
    else:
        type_name = str(annotation)

    # It's also possible to get the converter by type name
    type_struct = type_utils.get_canonical_type_name_for_type(type_name)
    if type_struct:
        return type_struct
    return type_name


def _maybe_make_unique(name: str, names: List[str]):
    if name not in names:
        return name

    for i in range(2, 100):
        unique_name = '{}_{}'.format(name, i)
        if unique_name not in names:
            return unique_name

    raise RuntimeError('Too many arguments with the name {}'.format(name))


def extract_component_interface(func: Callable) -> structures.ComponentSpec:
    single_output_name_const = 'Output'

    signature = inspect.signature(func)
    parameters = list(signature.parameters.values())

    parsed_docstring = docstring_parser.parse(inspect.getdoc(func))

    inputs = {}
    outputs = {}

    input_names = set()
    output_names = set()
    for parameter in parameters:
        parameter_type = type_annotations.maybe_strip_optional_from_annotation(
            parameter.annotation)
        passing_style = None
        io_name = parameter.name

        if type_annotations.is_artifact_annotation(parameter_type):
            # passing_style is either type_annotations.InputAnnotation or
            # type_annotations.OutputAnnotation.
            passing_style = type_annotations.get_io_artifact_annotation(
                parameter_type)

            # parameter_type is type_annotations.Artifact or one of its subclasses.
            parameter_type = type_annotations.get_io_artifact_class(
                parameter_type)
            if not issubclass(parameter_type, artifact_types.Artifact):
                raise ValueError(
                    'Input[T] and Output[T] are only supported when T is a '
                    'subclass of Artifact. Found `{} with type {}`'.format(
                        io_name, parameter_type))

            if parameter.default is not inspect.Parameter.empty:
                raise ValueError(
                    'Default values for Input/Output artifacts are not supported.'
                )
        elif isinstance(
                parameter_type,
            (type_annotations.InputPath, type_annotations.OutputPath)):
            passing_style = type(parameter_type)
            parameter_type = parameter_type.type
            if parameter.default is not inspect.Parameter.empty and not (
                    passing_style == type_annotations.InputPath and
                    parameter.default is None):
                raise ValueError(
                    'Path inputs only support default values of None. Default'
                    ' values for outputs are not supported.')

        type_struct = _annotation_to_type_struct(parameter_type)
        if type_struct is None:
            raise TypeError('Missing type annotation for argument: {}'.format(
                parameter.name))

        if passing_style in [
                type_annotations.OutputAnnotation, type_annotations.OutputPath
        ]:
            io_name = _maybe_make_unique(io_name, output_names)
            output_names.add(io_name)
            output_spec = structures.OutputSpec(type=type_struct)
            outputs[io_name] = output_spec
        else:
            io_name = _maybe_make_unique(io_name, input_names)
            input_names.add(io_name)
            if parameter.default is not inspect.Parameter.empty:
                input_spec = structures.InputSpec(
                    type=type_struct,
                    default=parameter.default,
                )
            else:
                input_spec = structures.InputSpec(type=type_struct)

            inputs[io_name] = input_spec

    #Analyzing the return type annotations.
    return_ann = signature.return_annotation
    if is_typed_named_tuple_annotation(return_ann):
        # Getting field type annotations.
        field_annotations = getattr(return_ann, '__annotations__', {})
        for field_name in return_ann._fields:
            type_struct = None
            if field_annotations:
                type_struct = _annotation_to_type_struct(
                    field_annotations.get(field_name, None))

            output_name = _maybe_make_unique(field_name, output_names)
            output_names.add(output_name)
            output_spec = structures.OutputSpec(type=type_struct)
            outputs[output_name] = output_spec
    # Deprecated dict-based way of declaring multiple outputs. Was only used by
    # the @component decorator
    elif isinstance(return_ann, dict):
        warnings.warn(
            'The ability to specify multiple outputs using the dict syntax'
            ' has been deprecated. It will be removed soon after release'
            ' 0.1.32. Please use typing.NamedTuple to declare multiple'
            ' outputs.')
        for output_name, output_type_annotation in return_ann.items():
            output_type_struct = _annotation_to_type_struct(
                output_type_annotation)
            output_spec = structures.OutputSpec(type=output_type_struct)
            outputs[name] = output_spec
    elif signature.return_annotation is not None and signature.return_annotation != inspect.Parameter.empty:
        output_name = _maybe_make_unique(single_output_name_const, output_names)
        # Fixes exotic, but possible collision:
        #   `def func(output_path: OutputPath()) -> str: ...`
        output_names.add(output_name)
        type_struct = _annotation_to_type_struct(signature.return_annotation)
        output_spec = structures.OutputSpec(type=type_struct)
        outputs[output_name] = output_spec

    # Component name and description are derived from the function's name and
    # docstring.  The name can be overridden by setting setting func.__name__
    # attribute (of the legacy func._component_human_name attribute).  The
    # description can be overridden by setting the func.__doc__ attribute (or
    # the legacy func._component_description attribute).
    component_name = getattr(func, '_component_human_name',
                             None) or _python_function_name_to_component_name(
                                 func.__name__)
    description = getattr(func, '_component_description',
                          None) or parsed_docstring.short_description
    if description:
        description = description.strip()

    component_spec = structures.ComponentSpec(
        name=component_name,
        description=description,
        inputs=inputs if inputs else None,
        outputs=outputs if outputs else None,
        # Dummy implementation to bypass model validation.
        implementation=structures.Implementation(),
    )
    return component_spec


def convert_arg_nodes_to_param_dict(args: List[ast.arg]) -> Dict[str, str]:
    arg_to_ann = {}
    for arg in args:
        annotation = arg.annotation

        if annotation is None:
            arg_to_ann[arg.arg] = None
        # Handle InputPath() and OutputPath()
        elif isinstance(annotation, ast.Call):
            if isinstance(annotation.func, ast.Name):
                arg_to_ann[arg.arg] = annotation.func.id
            elif isinstance(annotation.func, ast.Attribute):
                arg_to_ann[arg.arg] = annotation.func.value.id
            else:
                raise TypeError(
                    f'Unexpected type annotation for {annotation.func}.')
        # annotations with variable name
        elif isinstance(annotation, ast.Name):
            arg_to_ann[arg.arg] = annotation.id
        # annotations with a subtype like Input[Artifact]
        elif isinstance(annotation, ast.Subscript):
            # get inner type
            if PYTHON_VERSION < '3.9':
                # see "Changed in version 3.9" https://docs.python.org/3/library/ast.html#node-classes
                if isinstance(annotation.slice.value, ast.Name):
                    arg_to_ann[arg.arg] = annotation.slice.value.id
                if isinstance(annotation.slice.value, ast.Attribute):
                    arg_to_ann[arg.arg] = annotation.slice.value.value.id
            else:
                if isinstance(annotation.slice, ast.Name):
                    arg_to_ann[arg.arg] = annotation.slice.id
                if isinstance(annotation.slice, ast.Attribute):
                    arg_to_ann[arg.arg] = annotation.slice.value.id

        # annotations like type_annotations.Input[Artifact]
        elif isinstance(annotation, ast.Attribute):
            arg_to_ann[arg.arg] = annotation.value.id
        else:
            raise TypeError(
                f'Unexpected type annotation for {arg.arg}: {annotation}.')
    return arg_to_ann


def convert_return_ann_to_dict(return_ann: ast.Return) -> Dict[str, str]:
    return_dict = {}
    if return_ann is None:
        return_dict['return'] = None

    elif isinstance(return_ann, ast.Name):
        return_dict['return'] = return_ann.id

    # handles any sort of call in the return type
    elif isinstance(return_ann, ast.Call):
        func = return_ann.func

        # handles -> typing.NamedTuple
        if isinstance(
                func, ast.Attribute
        ) and func.value.id == 'typing' and func.attr == 'NamedTuple':
            nt_field_list = return_ann.args[1].elts
            for el in nt_field_list:
                return_dict[
                    f'return-{el.elts[0].s}'] = el.elts[1].id if isinstance(
                        el.elts[1], ast.Name) else el.elts[1].value.id

        # handles -> NamedTuple
        elif isinstance(func, ast.Name) and func.id == 'NamedTuple':
            nt_field_list = return_ann.args[1].elts
            for el in nt_field_list:
                return_dict[
                    f'return-{el.elts[0].s}'] = el.elts[1].id if isinstance(
                        el.elts[1], ast.Name) else el.elts[1].value.id

        elif isinstance(func, ast.Name):
            return_dict['return'] = func.id
        elif isinstance(func, ast.Attribute):
            return_dict['return'] = func.value.id
        else:
            raise TypeError(f'Unexpected type annotation for {func}.')

    # handles -> Output[Artifact]
    elif isinstance(return_ann, ast.Subscript):
        # get inner type
        if isinstance(return_ann.slice.value, ast.Name):
            return_dict['return'] = return_ann.slice.value.id
        if isinstance(return_ann.slice.value, ast.Attribute):
            return_dict['return'] = return_ann.slice.value.value.id
    else:
        raise TypeError(f'Unexpected return type annotation: {return_ann}.')
    return return_dict


def get_param_to_ann_string(function: Callable) -> Dict[str, str]:
    """Gets a dictionary of parameter name to type annotation string from a
    function. Note: Input[] and Output[] (typing.Annotated) are stripped from
    Artifact types.

    Args:
        func (Callable): The function.

    Returns:
        Dict[str, str]: Dictionary of parameter name to type annotation string.
    """
    module_node = ast.parse(_get_function_source_definition(function))
    args = module_node.body[0].args.args
    return_ann = module_node.body[0].returns
    param_dict = convert_arg_nodes_to_param_dict(args)
    return_dict = convert_return_ann_to_dict(return_ann)
    return {**param_dict, **return_dict}


def is_typed_named_tuple_annotation(annotation: Any) -> bool:
    """Checks if the type is a namedtuple (either typing.NamedTuple or
    collections.namedtuple).

    Args:
        annotation (Any): The type object.

    Returns:
        bool: Whether the type is a namedtuple type.
    """
    # it's possible to get false positives from this, but unlikely to be a problem for the KFP use-case unless someone creates a custom artifact class with the attribute ._fields
    if hasattr(annotation, '_fields'):
        is_typing_named_tuple = hasattr(annotation, '__annotations__')
        if not is_typing_named_tuple:
            raise TypeError(
                'Named tuple return types annotations must be typed with field annotations using typing.NamedTuple, rather than collections.namedtuple.'
            )
        return True
    return False


def get_param_to_ann_obj(func: Callable) -> Dict[str, Any]:
    """Gets a dictionary of parameter name to type annotation object from a
    function.

    Args:
        func (Callable): The function.

    Returns:
        Dict[str, Any]: Dictionary of parameter name to type annotation object.
    """
    signature = inspect.signature(func)
    param_to_ann_obj = {
        name: parameter.annotation
        for name, parameter in signature.parameters.items()
    }
    return_annotation = signature.return_annotation

    if is_typed_named_tuple_annotation(return_annotation):
        for name, annotation in return_annotation.__annotations__.items():
            param_to_ann_obj[f'return-{name}'] = annotation
    elif return_annotation is inspect.Signature.empty:
        param_to_ann_obj['return'] = None
    else:
        param_to_ann_obj['return'] = return_annotation

    return param_to_ann_obj


def get_full_qualname_for_artifact(obj: type) -> str:
    """Gets the fully qualified name for an annotation. For example, for class
    Foo in module bar.baz, this function returns bar.baz.Foo.

    Note: typing.get_type_hints purports to do the same thing, but it behaves differently when executed within the scope of a test, so preferring this approach instead.

    Args:
        obj (type): The class or module for which to get the fully qualified name.

    Returns:
        str: The fully qualified name for the class.
    """
    module = obj.__module__
    name = obj.__qualname__
    if module is not None and module != '__builtin__':
        name = module + '.' + name
    return name


def get_import_item_from_annotation_string_and_ann_obj(
        artifact_str: str, qualname: str) -> Dict[str, str]:
    """Get an import item dictionary (keys 'name' and 'alias') from an artifact
    annotation string.

    Args:
        artifact_str (str): The type annotation string.
        qualname (str): The fully qualified type annotation name as a string.

    Returns:
        Dict[str, str]: The string.
    """

    def is_camel_case(s: str) -> str:
        return s.islower() and s.isupper() and '_' not in s

    split_qualname = qualname.split('.')
    if artifact_str in split_qualname:
        name_to_import = '.'.join(
            split_qualname[:split_qualname.index(artifact_str) + 1])
    elif not is_camel_case(artifact_str):
        # It is challenging to handle module-relative type annotations (e.g. annotation_module.CustomArtifact) because the type annotation object doesn't hold a reference to its parent module (only the name as a string). If the pipeline author aliases a module import via `import module as alias`, we cannot determine the "true" module path to reconstruct an import. For now, module-relative imports with module aliases are considered not supported.
        warnings.warn(
            'It looks like you may be using a module-relative type annotation with an aliased module. This may be because of an import statement like `import module as alias`, then using an annotation like `alias.Class`. Module aliases are not supported in KFP component type annotations. Please use the original module name instead.',
            UserWarning)
        name_to_import = qualname
    else:
        name_to_import = qualname
    return {'name': name_to_import, 'alias': artifact_str}


def get_artifact_import_items_from_function(
        func: Callable) -> List[Dict[str, str]]:
    """Gets a list of required Artifact imports (keys 'name' and 'alias') from
    a function.

    Args:
        func (Callable): The component function.

    Returns:
        List[Dict[str, str]]: A dictionary containing string-to-string key-value pairs, with keys 'name' and 'alias'.
    """

    param_to_ann_string = get_param_to_ann_string(func)
    param_to_ann_obj = get_param_to_ann_obj(func)

    named_tuple_did_not_expand = 'return' in param_to_ann_string and 'return' not in param_to_ann_obj
    if named_tuple_did_not_expand:
        raise TypeError(
            "Error handling NamedTuple return type. The NamedTuple return type was assigned to a variable and referenced in the function signature. NamedTuple return types must be declared directly in the function signature like `-> NamedTuple('name', [('field', int)]):`."
        )

    import_items = []
    seen = set()
    for param_name, ann_string in param_to_ann_string.items():
        # don't process the same annotation string multiple times... us the string, not the object in the seen set, since the same object can be referenced different ways in the same function signature
        if ann_string in seen:
            continue
        else:
            seen.add(ann_string)

        actual_ann = param_to_ann_obj.get(param_name)
        if actual_ann is None:
            continue
        if type_annotations.is_artifact_annotation(actual_ann):
            artifact_class = type_annotations.get_io_artifact_class(actual_ann)
        elif inspect.isclass(actual_ann) and issubclass(
                actual_ann, artifact_types.Artifact):
            artifact_class = actual_ann
        else:
            continue

        artifact_qualname = get_full_qualname_for_artifact(artifact_class)
        if artifact_qualname.startswith('kfp.'):
            # kfp artifacts are already imported by default
            continue
        import_items.append(
            get_import_item_from_annotation_string_and_ann_obj(
                ann_string, artifact_qualname))
    return import_items


def import_artifact_annotations(import_items: List[Dict[str, str]],
                                globalns: Dict[str, Any]) -> None:
    """Imports objects to global namespace based on name and alias.
    mapping. For example, import_items = {'name': 'bar.baz.Foo', 'alias': 'Bat'} is equivalent to `from bar.baz import Foo as Bat`.

    Args:
        import_items (Dict[str, str]): A dictionary containing string-to-string key-value pairs, with keys 'name' and 'alias'.
        globalns (Dict[str, Any]): The global namespace dictionary.
    """
    for import_item in import_items:
        import_parts = import_item['name'].rsplit('.', 1)
        imported_obj = getattr(
            __import__(name=import_parts[0], fromlist=[import_parts[1]]),
            import_parts[1])
        globalns[import_item['alias']] = imported_obj


EXECUTOR_INPUT_PLACEHOLDER = "{{$}}"


def _get_command_and_args_for_lightweight_component(
        func: Callable) -> Tuple[List[str], List[str]]:
    items_to_import = get_artifact_import_items_from_function(func)
    imports_source = [
        'import kfp',
        'from kfp import dsl',
        'from kfp.dsl import *',
        'from typing import *',
        'from kfp.components import component_factory',
        f'component_factory.{import_artifact_annotations.__name__}({items_to_import}, globals())',
    ]

    func_source = _get_function_source_definition(func)
    source = textwrap.dedent('''
        {imports_source}

        {func_source}\n''').format(
        imports_source='\n'.join(imports_source), func_source=func_source)
    command = [
        'sh',
        '-ec',
        textwrap.dedent('''\
                    program_path=$(mktemp -d)
                    printf "%s" "$0" > "$program_path/ephemeral_component.py"
                    python3 -m kfp.components.executor_main \
                        --component_module_path \
                        "$program_path/ephemeral_component.py" \
                        "$@"
                '''),
        source,
    ]

    args = [
        '--executor_input',
        EXECUTOR_INPUT_PLACEHOLDER,
        '--function_to_execute',
        func.__name__,
    ]

    return command, args


def _get_command_and_args_for_containerized_component(
        function_name: str) -> Tuple[List[str], List[str]]:
    command = [
        'python3',
        '-m',
        'kfp.components.executor_main',
    ]

    args = [
        '--executor_input',
        EXECUTOR_INPUT_PLACEHOLDER,
        '--function_to_execute',
        function_name,
    ]
    return command, args


def create_component_from_func(func: Callable,
                               base_image: Optional[str] = None,
                               target_image: Optional[str] = None,
                               packages_to_install: List[str] = None,
                               pip_index_urls: Optional[List[str]] = None,
                               output_component_file: Optional[str] = None,
                               install_kfp_package: bool = True,
                               kfp_package_path: Optional[str] = None):
    """Implementation for the @component decorator.

    The decorator is defined under component_decorator.py. See the
    decorator for the canonical documentation for this function.
    """
    packages_to_install = packages_to_install or []

    if install_kfp_package and target_image is None:
        if kfp_package_path is None:
            kfp_package_path = _get_default_kfp_package_path()
        packages_to_install.append(kfp_package_path)

    packages_to_install_command = _get_packages_to_install_command(
        package_list=packages_to_install, pip_index_urls=pip_index_urls)

    command = []
    args = []
    if base_image is None:
        base_image = _DEFAULT_BASE_IMAGE

    component_image = base_image

    if target_image:
        component_image = target_image
        command, args = _get_command_and_args_for_containerized_component(
            function_name=func.__name__,)
    else:
        command, args = _get_command_and_args_for_lightweight_component(
            func=func)

    component_spec = extract_component_interface(func)
    component_spec.implementation = structures.Implementation(
        container=structures.ContainerSpec(
            image=component_image,
            command=packages_to_install_command + command,
            args=args,
        ))

    module_path = pathlib.Path(inspect.getsourcefile(func))
    module_path.resolve()

    component_name = _python_function_name_to_component_name(func.__name__)
    component_info = ComponentInfo(
        name=component_name,
        function_name=func.__name__,
        func=func,
        target_image=target_image,
        module_path=module_path,
        component_spec=component_spec,
        output_component_file=output_component_file,
        base_image=base_image)

    if REGISTERED_MODULES is not None:
        REGISTERED_MODULES[component_name] = component_info

    if output_component_file:
        component_spec.save_to_component_yaml(output_component_file)

    return python_component.PythonComponent(
        component_spec=component_spec, python_func=func)
