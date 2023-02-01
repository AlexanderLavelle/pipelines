
# Copyright 2021 The Kubeflow Authors. All Rights Reserved.
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
"""Setup configuration of  Google Cloud Pipeline Components client side libraries."""


def make_required_install_packages():
    return [
        # To resolve RTD errors with "No module named 'google.cloud.location'"
        "googleapis-common-protos>=1.56.2,<2.0dev",
        # Pin google-api-core version for the bug fixing in 1.31.5
        # https://github.com/googleapis/python-api-core/releases/tag/v1.31.5
        "google-api-core>=1.31.5,<3.0.0dev,!=2.0.*,!=2.1.*,!=2.2.*,!=2.3.*,!=2.4.*,!=2.5.*,!=2.6.*,!=2.7.*",
        # To resolve RTD errors with error: protobuf<4.0.0dev,>=3.19.0 is required by {'google-cloud-aiplatform'}
        "protobuf<4.0.0dev,>=3.19.0",
        "grpcio-status<=1.47.0",
        "google-cloud-storage<3,>=2.2.1",
        "kfp>=1.8.9,<2.0.0",
        "google-cloud-notebooks>=0.4.0",
        "google-cloud-aiplatform>=1.14.0,<2",
    ]

def make_required_test_packages():
    return make_required_install_packages() + [
        "mock>=4.0.0",
        "flake8>=3.0.0",
        "pytest>=6.0.0",
    ]


def make_dependency_links():
    return []
