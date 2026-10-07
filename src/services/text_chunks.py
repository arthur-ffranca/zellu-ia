"""Text records and bounded, overlapping chunks for the native Pinecone index."""
from dataclasses import dataclass, field
from copy import deepcopy


@dataclass
class TextDocument:
    page_content: str
    metadata: dict = field(default_factory=dict)


class TextChunker:
    def __init__(self, chunk_size=1000, chunk_overlap=200, separators=None, length_function=len):
        if chunk_size <= 0 or not 0 <= chunk_overlap < chunk_size:
            raise ValueError("Require 0 <= overlap < chunk_size")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.separators = separators or ["\n\n", "\n", ". ", " "]

    def split_text(self, text):
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + self.chunk_size, len(text))
            if end < len(text):
                for separator in self.separators:
                    if not separator:
                        continue
                    split = text.rfind(separator, start + self.chunk_overlap + 1, end)
                    if split >= 0:
                        end = split + len(separator)
                        break
            chunk = text[start:end]
            if chunk.strip():
                chunks.append(chunk)
            if end == len(text):
                break
            start = max(start + 1, end - self.chunk_overlap)
        return chunks

    def split_documents(self, documents):
        return [TextDocument(chunk, deepcopy(doc.metadata))
                for doc in documents for chunk in self.split_text(doc.page_content)]

    def create_documents(self, texts, metadatas=None):
        return self.split_documents([TextDocument(text, (metadatas or [{}] * len(texts))[i])
                                     for i, text in enumerate(texts)])
