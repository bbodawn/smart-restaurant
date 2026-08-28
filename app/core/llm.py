import os
from typing import Type, TypeVar
from pydantic import BaseModel
from langchain_ollama import ChatOllama

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

T = TypeVar("T", bound=BaseModel)

def get_llm(temperature: float = 0.0) -> ChatOllama:
    return ChatOllama(
        model=OLLAMA_MODEL,
        base_url=OLLAMA_BASE_URL,
        temperature=temperature,
    )

def get_structured_llm(schema_cls: Type[T], temperature: float = 0.0):
    llm = get_llm(temperature=temperature)
    return llm.with_structured_output(schema_cls)
