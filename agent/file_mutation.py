"""Shared file mutation operation vocabulary."""
from typing import Literal

FileMutationOperation = Literal["create", "modify", "overwrite", "delete"]
WriteOperation = Literal["create", "overwrite"]
