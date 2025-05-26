import os
import numpy as np
from sklearn.manifold import TSNE
import matplotlib.pyplot as plt
from sklearn.metrics.pairwise import cosine_similarity
from FlagEmbedding import BGEM3FlagModel
from FlagEmbedding.abc.inference.AbsEmbedder import AbsEmbedder

class RagSystem:
    """
    Classe per indicizzare, cercare e visualizzare embeddings di testo usando BGEM3FlagModel.

    Parametri:
        txt_file (str): Percorso al file di testo contenente le frasi, una per riga.
        emb_file (str): Percorso dove salvare/caricare la matrice di embeddings (.npy).
        model_name (str): Nome del modello da caricare (es. 'BAAI/bge-m3').
        use_fp16 (bool): Se usare precisione fp16 per il modello.
        reindex (bool): Se rigenerare sempre gli embeddings.
    """
    def __init__(self,
                 txt_file: str,
                 emb_file: str = "embeddings.npy",
                 model_name: str = 'BAAI/bge-m3',
                 use_fp16: bool = True,
                 reindex: bool = False):
        self.txt_file = txt_file
        self.emb_file = emb_file
        self.reindex = reindex

        AbsEmbedder.__del__ = lambda self: None  # type: ignore
        self.model = BGEM3FlagModel(model_name, use_fp16=use_fp16)
        print(f"[+] Modello '{model_name}' caricato (use_fp16={use_fp16})")

    def _read_data(self) -> list[str]:
        if not os.path.exists(self.txt_file):
            raise FileNotFoundError(f"File '{self.txt_file}' non trovato!")
        with open(self.txt_file, 'r', encoding='utf-8') as f:
            data = [line.strip() for line in f if line.strip()]
        print(f"[+] Letto {len(data)} frasi da '{self.txt_file}'")
        return data

    def index_database(self, data: list[str] | None = None) -> np.ndarray:
        if data is None:
            data = self._read_data()
        embeddings = self.model.encode(data)['dense_vecs']
        np.save(self.emb_file, embeddings)
        print(f"[+] Embeddings salvati in '{self.emb_file}' (shape={embeddings.shape})")
        return embeddings

    def load_embedding_matrix(self) -> np.ndarray:
        if not os.path.exists(self.emb_file):
            raise FileNotFoundError(f"File '{self.emb_file}' non trovato! Esegui prima index_database().")
        embeddings = np.load(self.emb_file)
        print(f"[+] Embeddings caricati da '{self.emb_file}' (shape={embeddings.shape})")
        return embeddings

    def search(self, query: str, embedding_matrix: np.ndarray) -> list[tuple[int, float]]:
        query_emb = self.model.encode([query])['dense_vecs'][0]
        if query_emb.shape[0] != embedding_matrix.shape[1]:
            raise ValueError(f"Dimensione embedding query ({query_emb.shape[0]}) incompatibile con matrix ({embedding_matrix.shape[1]})")
        sims = cosine_similarity([query_emb], embedding_matrix)[0]
        return sorted(enumerate(sims), key=lambda x: x[1], reverse=True)

    def visualize_space_query(self,
                              data: list[str],
                              query: str,
                              embedding_matrix: np.ndarray,
                              perplexity: int = 2,
                              random_state: int = 42) -> None:
        query_emb = self.model.encode([query])['dense_vecs'][0]
        if query_emb.shape[0] != embedding_matrix.shape[1]:
            raise ValueError("Dimensione embedding mismatch per t-SNE visualizzazione.")
        joint = np.vstack([embedding_matrix, query_emb])
        tsne = TSNE(n_components=2, perplexity=perplexity, random_state=random_state)
        emb2d = tsne.fit_transform(joint)

        plt.figure(figsize=(8, 6))
        plt.scatter(emb2d[:-1, 0], emb2d[:-1, 1], edgecolor='k', label='Frasi')
        plt.scatter(emb2d[-1, 0], emb2d[-1, 1], edgecolor='k', label='Query', c='red')
        for i, frase in enumerate(data):
            plt.text(emb2d[i, 0] + 0.1, emb2d[i, 1] + 0.1, frase, fontsize=9)
        plt.text(emb2d[-1, 0] + 0.1, emb2d[-1, 1] + 0.1, query, fontsize=9, color='red')
        plt.title('Visualizzazione degli Embeddings con t-SNE')
        plt.xlabel('Dimensione 1')
        plt.ylabel('Dimensione 2')
        plt.grid(True)
        plt.legend()
        plt.show()

    def run(self, query: str, top_k: int = 5, visualize: bool = False) -> None:
        data = self._read_data()
        emb = None
        # Carica o rigenera embeddings
        if self.reindex or not os.path.exists(self.emb_file):
            emb = self.index_database(data)
        else:
            emb = self.load_embedding_matrix()
            # Verifica compatibilità dimensionale
            query_emb = self.model.encode([data[0]])['dense_vecs'][0]
            if query_emb.shape[0] != emb.shape[1]:
                print(f"[!] Dimensione embedding cambiata ({query_emb.shape[0]} vs {emb.shape[1]}), rigenero database...")
                emb = self.index_database(data)

        # Ricerca
        results = self.search(query, emb)
        print(f"\nTop-{top_k} frasi più simili a '{query}':")
        for idx, score in results[:top_k]:
            print(f"  [{idx}] (score={score:.4f}): {data[idx]}")

        # Visualizzazione
        if visualize:
            self.visualize_space_query(data, query, emb)


def main():
    searcher = RagSystem(
        txt_file="private/input.txt",
        emb_file="embeddings.npy",
        model_name='BAAI/bge-m3',
        use_fp16=True,
        reindex=False
    )
    searcher.run(query="how many drivers?", top_k=5, visualize=False)

if __name__ == "__main__":
    main()
