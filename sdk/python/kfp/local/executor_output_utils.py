# Copyright 2023 The Kubeflow Authors
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
"""Utilities for reading and processing the ExecutorOutput message."""
import json
import os
from typing import Any, Dict, List, Union

from google.protobuf import json_format
from google.protobuf import struct_pb2
from kfp import dsl
from kfp.compiler import pipeline_spec_builder
from kfp.pipeline_spec import pipeline_spec_pb2


def load_executor_output(
        executor_output_path: str) -> pipeline_spec_pb2.ExecutorOutput:
    """Loads the ExecutorOutput message from a path.

    Args:
        executor_output_path: The file path.

    Returns:
        The ExecutorOutput message.
    """
    executor_output = pipeline_spec_pb2.ExecutorOutput()
    with open(executor_output_path) as f:
        json_format.Parse(f.read(), executor_output)
    return executor_output


def cast_floats_as_ints_to_floats(
    outputs_dict: Dict[str, Any],
    outputs_typed_int: List[str],
) -> Dict[str, Any]:
    """Casts output fields that are typed as NUMBER_INTEGER to a Python int.

    This is required, since all google.protobuf.struct_pb2.Value uses
    number_value to support both floats and ints. When converting
    struct_pb2.Value to a dict/json, int will be upcast to float, even
    if the component output specifies int.
    """
    for float_output_key in outputs_typed_int:
        outputs_dict[float_output_key] = int(outputs_dict[float_output_key])
    return outputs_dict


def get_outputs_from_executor_output(
    executor_output: pipeline_spec_pb2.ExecutorOutput,
    executor_input: pipeline_spec_pb2.ExecutorInput,
    component_spec: pipeline_spec_pb2.ComponentSpec,
) -> Dict[str, Any]:
    """Obtains a dictionary of output key to output value from several
    PipelineSpec messages.

    Used for creating LocalTask outputs.
    """
    executor_output = add_type_to_executor_output(
        executor_input=executor_input,
        executor_output=executor_output,
    )

    # merge any parameter outputs written using dsl.OutputPath with the rest of ExecutorOutput
    executor_output = merge_dsl_output_file_parameters_to_executor_output(
        executor_input=executor_input,
        executor_output=executor_output,
        component_spec=component_spec,
    )

    # collect outputs from executor output
    output_parameters = {
        param_name: pb2_value_to_python(value)
        for param_name, value in executor_output.parameter_values.items()
    }
    output_artifact_definitions = component_spec.output_definitions.artifacts
    output_artifacts = {
        artifact_name: artifact_list_to_dsl_artifact(
            artifact_list,
            is_artifact_list=output_artifact_definitions[artifact_name]
            .is_artifact_list,
        ) for artifact_name, artifact_list in executor_output.artifacts.items()
    }
    outputs_dict = {**output_parameters, **output_artifacts}

    # process the special case of protobuf ints
    outputs_typed_int = [
        output_param_name for output_param_name, parameter_spec in
        component_spec.output_definitions.parameters.items()
        if parameter_spec.parameter_type ==
        pipeline_spec_pb2.ParameterType.ParameterTypeEnum.NUMBER_INTEGER
    ]
    outputs_dict = cast_floats_as_ints_to_floats(
        outputs_dict,
        outputs_typed_int,
    )
    return outputs_dict


def special_dsl_outputpath_read(output_file: str, is_string: bool) -> Any:
    """Reads the text in dsl.OutputPath files in the same way as the remote
    backend.

    Basically deserialize all types as JSON, but also support strings
    that are written directly without quotes (e.g., `foo` instead of
    `"foo"`).
    """
    with open(output_file) as f:
        parameter_value = f.read()
    # TODO: what should the special handling be?
    return parameter_value if is_string else json.loads(parameter_value)


def merge_dsl_output_file_parameters_to_executor_output(
    executor_input: pipeline_spec_pb2.ExecutorInput,
    executor_output: pipeline_spec_pb2.ExecutorOutput,
    component_spec: pipeline_spec_pb2.ComponentSpec,
) -> pipeline_spec_pb2.ExecutorOutput:
    for parameter_key, output_parameter in executor_input.outputs.parameters.items(
    ):
        if os.path.exists(output_parameter.output_file):
            is_string = component_spec.output_definitions.parameters[
                parameter_key].parameter_type == pipeline_spec_pb2.ParameterType.ParameterTypeEnum.STRING
            parameter_value = special_dsl_outputpath_read(
                output_parameter.output_file,
                is_string,
            )
            executor_output.parameter_values[parameter_key].CopyFrom(
                pipeline_spec_builder.to_protobuf_value(parameter_value))

    return executor_output


def pb2_value_to_python(value: struct_pb2.Value) -> Any:
    """Converts protobuf Value to the corresponding Python type."""
    if value.HasField('null_value'):
        return None
    elif value.HasField('number_value'):
        return value.number_value
    elif value.HasField('string_value'):
        return value.string_value
    elif value.HasField('bool_value'):
        return value.bool_value
    elif value.HasField('struct_value'):
        return pb2_struct_to_python(value.struct_value)
    elif value.HasField('list_value'):
        return [pb2_value_to_python(v) for v in value.list_value.values]
    else:
        raise ValueError(f'Unknown value type: {value}')


def pb2_struct_to_python(struct):
    """Converts protobuf Struct to a dict."""
    return {k: pb2_value_to_python(v) for k, v in struct.fields.items()}


def runtime_artifact_to_dsl_artifact(
        runtime_artifact: pipeline_spec_pb2.RuntimeArtifact) -> dsl.Artifact:
    """Converts a single RuntimeArtifact instance to the corresponding
    dsl.Artifact instance."""
    from kfp.dsl import executor
    return executor.create_artifact_instance(
        json_format.MessageToDict(runtime_artifact))


def artifact_list_to_dsl_artifact(
    artifact_list: pipeline_spec_pb2.ArtifactList,
    is_artifact_list: bool,
) -> Union[dsl.Artifact, List[dsl.Artifact]]:
    """Converts an ArtifactList instance to a single dsl.Artifact or a list of
    dsl.Artifacts, depending on thether the ArtifactList is a true list or
    simply a container for single element."""
    dsl_artifacts = [
        runtime_artifact_to_dsl_artifact(artifact_spec)
        for artifact_spec in artifact_list.artifacts
    ]
    return dsl_artifacts if is_artifact_list else dsl_artifacts[0]


def add_type_to_executor_output(
    executor_input: pipeline_spec_pb2.ExecutorInput,
    executor_output: pipeline_spec_pb2.ExecutorOutput,
) -> pipeline_spec_pb2.ExecutorOutput:
    """Adds artifact type information (ArtifactTypeSchema) from the
    ExecutorInput message to the ExecutorOutput message.

    This information is not present in the serialized ExecutorOutput message, though it would be useful to have it for constructing LocalTask outputs (accessed via task.outputs['foo']). We don't want to change the serialized output, however, and introduce differences between local and cloud execution.

    To simplify the local implementation, we add this extra info to ExecutorOutput.
    """
    for key, artifact_list in executor_output.artifacts.items():
        for artifact in artifact_list.artifacts:
            artifact.type.CopyFrom(
                executor_input.outputs.artifacts[key].artifacts[0].type)
    return executor_output


def get_outputs_from_messages(
    executor_input: pipeline_spec_pb2.ExecutorInput,
    component_spec: pipeline_spec_pb2.ComponentSpec,
) -> Dict[str, Any]:
    """Gets outputs from a recently executed task (where outputs are available)
    using the ExecutorInput and ComponentSpec of the task."""
    executor_output = load_executor_output(
        executor_output_path=executor_input.outputs.output_file)
    return get_outputs_from_executor_output(
        executor_output=executor_output,
        executor_input=executor_input,
        component_spec=component_spec,
    )
