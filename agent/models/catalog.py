"""Model lookup policy, independent of YAML configuration."""

from dataclasses import dataclass

from agent.models.profile import ModelProfile


@dataclass(frozen=True)
class ModelCatalog:
    profiles: tuple[ModelProfile, ...]
    default_id: str

    @property
    def active_profile(self) -> ModelProfile:
        return self.get_profile(self.default_id)

    def get_profile(self, id_or_prefix: str) -> ModelProfile:
        key = id_or_prefix.strip()
        if not key:
            raise KeyError("model id is empty")
        exact = next((item for item in self.profiles if item.id == key), None)
        if exact is not None:
            return exact
        matches = [item for item in self.profiles if item.id.startswith(key)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise KeyError(f"Unknown model: {id_or_prefix}")
        ids = ", ".join(item.id for item in matches)
        raise KeyError(f"Ambiguous model prefix {id_or_prefix!r}: {ids}")
