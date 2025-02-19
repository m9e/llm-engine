from abc import ABC, abstractmethod
from typing import List


class LLMArtifactGateway(ABC):
    """
    Abstract Base Class for interacting with llm artifacts.
    """

    @abstractmethod
    def list_files(self, path: str, **kwargs) -> List[str]:
        """
        Gets a list of files from a given path.
        """
        pass

    @abstractmethod
    def get_model_weights_urls(self, owner: str, model_name: str, **kwargs) -> List[str]:
        """
        Gets a list of URLs for all files associated with a given model.
        """
        pass
