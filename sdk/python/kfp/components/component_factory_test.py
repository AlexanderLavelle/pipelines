# Copyright 2022 The Kubeflow Authors
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

import inspect
import os
import sys
import tempfile
import textwrap
import typing
from typing import Any
import unittest
from kfp import compiler
from absl.testing import parameterized
from kfp.components import component_factory
from kfp.components.types import artifact_types
from kfp.components.types import type_annotations
from kfp.components.types.artifact_types import Artifact
from kfp.components.types.artifact_types import Dataset
from kfp.components.types.type_annotations import Input
from kfp.components.types.type_annotations import InputPath
from kfp.components.types.type_annotations import Output
from kfp.components.types.type_annotations import OutputPath
import typing_extensions
from kfp import dsl
import yaml


class TestGetPackagesToInstallCommand(unittest.TestCase):

    def test_with_no_packages_to_install(self):
        packages_to_install = []

        command = component_factory._get_packages_to_install_command(
            packages_to_install)
        self.assertEqual(command, [])

    def test_with_packages_to_install_and_no_pip_index_url(self):
        packages_to_install = ['package1', 'package2']

        command = component_factory._get_packages_to_install_command(
            packages_to_install)
        concat_command = ' '.join(command)
        for package in packages_to_install:
            self.assertTrue(package in concat_command)

    def test_with_packages_to_install_with_pip_index_url(self):
        packages_to_install = ['package1', 'package2']
        pip_index_urls = ['https://myurl.org/simple']

        command = component_factory._get_packages_to_install_command(
            packages_to_install, pip_index_urls)
        concat_command = ' '.join(command)
        for package in packages_to_install + pip_index_urls:
            self.assertTrue(package in concat_command)


Alias = Artifact
artifact_types_alias = artifact_types


class TestGetParamToAnnString(unittest.TestCase):

    def test_no_annotations(self):

        def func(a, b):
            pass

        actual = component_factory.get_param_to_annotation_string_for_artifacts(
            func)
        expected = {}
        self.assertEqual(actual, expected)

    def test_no_return(self):

        def func(a: int, b: Input[Artifact]):
            pass

        actual = component_factory.get_param_to_annotation_string_for_artifacts(
            func)
        expected = {'b': 'Artifact'}
        self.assertEqual(actual, expected)

    def test_with_return(self):

        def func(a: int, b: Input[Artifact]) -> int:
            return 1

        actual = component_factory.get_param_to_annotation_string_for_artifacts(
            func)
        expected = {'b': 'Artifact'}
        self.assertEqual(actual, expected)

    def test_multiline(self):

        def func(
            a: int,
            b: Input[Artifact],
        ) -> int:
            return 1

        actual = component_factory.get_param_to_annotation_string_for_artifacts(
            func)
        expected = {'b': 'Artifact'}
        self.assertEqual(actual, expected)

    def test_alias(self):

        def func(a: int, b: Input[Alias]):
            pass

        actual = component_factory.get_param_to_annotation_string_for_artifacts(
            func)
        expected = {'b': 'Alias'}
        self.assertEqual(actual, expected)

    def test_long_form_annotation(self):

        def func(a: int, b: Input[artifact_types.Artifact]):
            pass

        actual = component_factory.get_param_to_annotation_string_for_artifacts(
            func)
        expected = {'b': 'artifact_types'}
        self.assertEqual(actual, expected)

    def test_named_tuple(self):

        def func(
            a: int,
            b: Input[Artifact],
        ) -> typing.NamedTuple('MyNamedTuple', [('a', int), (
                'b', Artifact), ('c', artifact_types.Artifact)]):
            InnerNamedTuple = typing.NamedTuple(
                'MyNamedTuple', [('a', int), ('b', Artifact),
                                 ('c', artifact_types.Artifact)])
            return InnerNamedTuple(a=a, b=b, c=b)  # type: ignore

        actual = component_factory.get_param_to_annotation_string_for_artifacts(
            func)
        expected = {'b': 'Artifact'}
        self.assertEqual(actual, expected)

    def test_input_output_path(self):

        def func(
                a: int,
                b: InputPath('Dataset'),
        ) -> OutputPath('Dataset'):
            return 'dataset'

        actual = component_factory.get_param_to_annotation_string_for_artifacts(
            func)
        expected = {'b': 'InputPath'}
        self.assertEqual(actual, expected)


class MyCustomArtifact:
    TYPE_NAME = 'my_custom_artifact'


class _TestCaseWithThirdPartyPackage(parameterized.TestCase):

    @classmethod
    def setUpClass(cls):

        class ThirdPartyArtifact:
            TYPE_NAME = 'custom.my_third_party_artifact'

        class_source = textwrap.dedent(inspect.getsource(ThirdPartyArtifact))

        tmp_dir = tempfile.TemporaryDirectory()
        with open(os.path.join(tmp_dir.name, 'my_package.py'), 'w') as f:
            f.write(class_source)
        sys.path.append(tmp_dir.name)
        cls.tmp_dir = tmp_dir

    @classmethod
    def teardownClass(cls):
        sys.path.pop()
        cls.tmp_dir.cleanup()


class TestGetParamToAnnObj(unittest.TestCase):

    def test_no_named_tuple(self):

        def func(
            a: int,
            b: Input[Artifact],
        ) -> int:
            return 1

        actual = component_factory.get_param_to_annotation_object(func)
        expected = {
            'a':
                int,
            'b':
                typing_extensions.Annotated[Artifact,
                                            type_annotations.InputAnnotation]
        }
        self.assertEqual(actual, expected)

    def test_named_tuple(self):

        MyNamedTuple = typing.NamedTuple('MyNamedTuple', [('a', int),
                                                          ('b', str)])

        def func(
            a: int,
            b: Input[Artifact],
        ) -> MyNamedTuple:
            InnerNamedTuple = typing.NamedTuple('MyNamedTuple', [('a', int),
                                                                 ('b', str)])
            return InnerNamedTuple(a=a, b='string')  # type: ignore

        actual = component_factory.get_param_to_annotation_object(func)
        expected = {
            'a':
                int,
            'b':
                typing_extensions.Annotated[Artifact,
                                            type_annotations.InputAnnotation]
        }
        self.assertEqual(actual, expected)

    def test_input_output_path(self):

        def func(
                a: int,
                b: InputPath('Dataset'),
        ) -> OutputPath('Dataset'):
            return 'dataset'

        actual = component_factory.get_param_to_annotation_object(func)
        self.assertEqual(actual['a'], int)
        self.assertIsInstance(actual['b'], InputPath)


class TestGetFullQualnameForClass(_TestCaseWithThirdPartyPackage):

    @parameterized.parameters([
        (Alias, 'kfp.components.types.artifact_types.Artifact'),
        (Artifact, 'kfp.components.types.artifact_types.Artifact'),
        (Dataset, 'kfp.components.types.artifact_types.Dataset'),
    ])
    def test(self, obj: Any, expected_qualname: str):
        self.assertEqual(
            component_factory.get_full_qualname_for_object(obj),
            expected_qualname)

    def test_my_package_artifact(self):
        import my_package
        self.assertEqual(
            component_factory.get_full_qualname_for_object(
                my_package.ThirdPartyArtifact), 'my_package.ThirdPartyArtifact')


class GetArtifactImportItemsFromFunction(_TestCaseWithThirdPartyPackage):

    def test_no_annotations(self):

        def func(a, b):
            pass

        actual = component_factory.get_artifact_import_items_from_function(func)
        expected = []
        self.assertEqual(actual, expected)

    def test_no_return(self):
        from my_package import ThirdPartyArtifact

        def func(a: int, b: Input[ThirdPartyArtifact]):
            pass

        actual = component_factory.get_artifact_import_items_from_function(func)
        expected = ['my_package.ThirdPartyArtifact']
        self.assertEqual(actual, expected)

    def test_with_return(self):
        from my_package import ThirdPartyArtifact

        def func(a: int, b: Input[ThirdPartyArtifact]) -> int:
            return 1

        actual = component_factory.get_artifact_import_items_from_function(func)
        expected = ['my_package.ThirdPartyArtifact']
        self.assertEqual(actual, expected)

    def test_multiline(self):
        from my_package import ThirdPartyArtifact

        def func(
            a: int,
            b: Input[ThirdPartyArtifact],
        ) -> int:
            return 1

        actual = component_factory.get_artifact_import_items_from_function(func)
        expected = ['my_package.ThirdPartyArtifact']
        self.assertEqual(actual, expected)

    def test_alias(self):
        from my_package import ThirdPartyArtifact
        Alias = ThirdPartyArtifact

        def func(a: int, b: Input[Alias]):
            pass

        with self.assertRaisesRegex(
                TypeError, r'Module or type name aliases are not supported'):
            component_factory.get_artifact_import_items_from_function(func)

    def test_long_form_annotation(self):
        import my_package

        def func(a: int, b: Output[my_package.ThirdPartyArtifact]):
            pass

        actual = component_factory.get_artifact_import_items_from_function(func)
        expected = ['my_package']
        self.assertEqual(actual, expected)

    def test_aliased_module_throws_error(self):
        import my_package as my_package_alias

        def func(a: int, b: Output[my_package_alias.ThirdPartyArtifact]):
            pass

        with self.assertRaisesRegex(
                TypeError, r'Module or type name aliases are not supported'):
            component_factory.get_artifact_import_items_from_function(func)

    def test_input_output_path(self):
        from my_package import ThirdPartyArtifact

        def func(
            a: int,
            b: InputPath('Dataset'),
            c: Output[ThirdPartyArtifact],
        ) -> OutputPath('Dataset'):
            return 'dataset'

        actual = component_factory.get_artifact_import_items_from_function(func)
        self.assertEqual(actual, ['my_package.ThirdPartyArtifact'])


class TestImportStatementAdded(_TestCaseWithThirdPartyPackage):

    def test(self):
        from my_package import ThirdPartyArtifact
        import my_package

        @dsl.component
        def one(
            a: int,
            b: Output[ThirdPartyArtifact],
        ):
            pass

        @dsl.component
        def two(a: Input[my_package.ThirdPartyArtifact],):
            pass

        @dsl.pipeline()
        def my_pipeline():
            one_task = one(a=1)
            two_task = two(a=one_task.outputs['b'])

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, 'pipeline.yaml')

            compiler.Compiler().compile(
                pipeline_func=my_pipeline, package_path=output_path)
            with open(output_path) as f:
                pipeline_spec = yaml.safe_load(f)
        self.assertIn(
            'from my_package import ThirdPartyArtifact',
            ' '.join(pipeline_spec['deploymentSpec']['executors']['exec-one']
                     ['container']['command']))
        self.assertIn(
            'import my_package',
            ' '.join(pipeline_spec['deploymentSpec']['executors']['exec-two']
                     ['container']['command']))


if __name__ == '__main__':
    unittest.main()
