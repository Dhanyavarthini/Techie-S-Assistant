"""Copyright 2023 SambaNova Systems, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from pathlib import Path
from typing import Optional

import pytest

from generative_data_prep.tokenized_line import (
    Token,
    TokenizedArticle,
    TokenizedLine,
    TokenizedSequence,
)

PYTHON_ROOT_DIR = str(Path(__file__).parent.parent.absolute())
TESTS_EXAMPLES_PATH = Path(__file__).parent / "examples"


def get_tokenized_line(length: int, max_seq_length: Optional[int], eos_token_id: Optional[int]) -> TokenizedLine:
    """Create either a tokenized article or a tokenized sequence based on the arguments.

    The token_ids are initialized to be [0, 1, 2, ... length - 1]
    The token_type_ids are initialized to be [0, -1, -2, ... 1 - length]
    """
    are_nones = [val is None for val in (max_seq_length, eos_token_id)]
    token_ids = list(range(length))
    token_type_ids = list(range(0, -length, -1))

    tokens = list(map(lambda x: Token(x[0], x[1]), zip(token_ids, token_type_ids)))

    if all(are_nones):
        tokenized_line: TokenizedLine = TokenizedArticle(tokens)
    elif not any(are_nones):
        # required for mypy
        assert max_seq_length is not None and eos_token_id is not None
        tokenized_line = TokenizedSequence(tokens, max_seq_length, eos_token_id)
    else:
        raise ValueError("Both max_seq_length and eos_token_id must be None or both must be non-None")

    return tokenized_line


@pytest.fixture
def tokenized_line(length: int, max_seq_length: Optional[int], eos_token_id: Optional[int]) -> TokenizedLine:
    """Creates a tokenized line."""
    return get_tokenized_line(length, max_seq_length, eos_token_id)


@pytest.fixture
def tokenized_line_2(length_2, max_seq_length_2, eos_token_id_2):
    """Creates a tokenized line.  To be used if you've already used the other tokenized line fixture
    in your test function.
    """
    return get_tokenized_line(length_2, max_seq_length_2, eos_token_id_2)
