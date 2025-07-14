import os
import time
import logging
import numpy as np
import gc
import torch
import re
from sentence_transformers import SentenceTransformer

# Configurazione logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('rag_debug.log', mode='w', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

class RagSystem:
    def __init__(self,
                 txt_dir: str = "uploads",
                 emb_file: str = "embeddings.npz",
                 model_name: str = './models/all-MiniLM-L6-v2',
                 reindex: bool = False):
        logger.debug(f"Inizializzazione RagSystem - txt_dir: {txt_dir}, emb_file: {emb_file}, model_name: {model_name}, reindex: {reindex}")
        start_time = time.time()

        self.txt_dir = txt_dir
        self.emb_file = emb_file
        self.reindex = reindex

        logger.debug("Configurazione ottimizzazioni per dispositivi low-end")
        os.environ['TOKENIZERS_PARALLELISM'] = 'false'
        if torch.cuda.is_available():
            logger.info("CUDA disponibile ma forziamo CPU per stabilità su Jetson Nano")

        # Forza il percorso assoluto per evitare che SentenceTransformer provi a scaricare
        model_path = os.path.abspath(model_name)
        logger.debug(f"Inizio caricamento modello locale: {model_path}")
        model_start = time.time()

        try:
            self.model = SentenceTransformer(model_path, device='cpu')
            self.model.eval()
        except Exception as e:
            logger.error(f"Errore nel caricamento del modello locale: {e}")
            raise FileNotFoundError(
                f"Impossibile caricare il modello da '{model_path}'. Verifica che contenga i file richiesti per l'uso offline."
            )

        model_end = time.time()
        logger.info(f"Modello '{model_path}' caricato in {model_end - model_start:.2f} secondi")
        logger.debug(f"Inizializzazione completata in {time.time() - start_time:.2f} secondi")

    def _read_data(self) -> list[str]:
        logger.debug(f"Inizio lettura directory: {self.txt_dir}")
        start_time = time.time()

        if not os.path.exists(self.txt_dir) or not os.path.isdir(self.txt_dir):
            raise FileNotFoundError(f"Directory '{self.txt_dir}' non trovata!")

        txt_files = [f for f in sorted(os.listdir(self.txt_dir)) if f.lower().endswith('.txt')]
        if not txt_files:
            raise FileNotFoundError(f"Nessun file .txt trovato in {self.txt_dir}")

        all_text = []
        for fname in txt_files:
            with open(os.path.join(self.txt_dir, fname), 'r', encoding='utf-8') as f:
                all_text.append(f.read())

        combined_text = '\n'.join(all_text)
        sentences = [s.strip() for s in re.split(r'(?<=[\.!?])\s+', combined_text) if s.strip()]

        logger.info(f"Lette {len(sentences)} frasi da {len(txt_files)} file in {time.time() - start_time:.2f} secondi")
        return sentences

    def index_database(self, data: list[str] | None = None) -> np.ndarray:
        logger.debug("Inizio indicizzazione database")
        start_time = time.time()

        if data is None:
            data = self._read_data()

        embeddings_list = []
        with torch.no_grad():
            for i in range(0, len(data), 16):
                batch = data[i:i+16]
                batch_embeddings = self.model.encode(
                    batch,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                    batch_size=16,
                    normalize_embeddings=True
                )
                embeddings_list.append(batch_embeddings)
                if i % 64 == 0:
                    gc.collect()

        embeddings = np.vstack(embeddings_list)
        np.savez_compressed(self.emb_file, embeddings=embeddings)

        logger.info(f"Indicizzazione completata in {time.time() - start_time:.2f} secondi (shape={embeddings.shape})")
        return embeddings

    def load_embedding_matrix(self) -> np.ndarray:
        if not os.path.exists(self.emb_file):
            raise FileNotFoundError(f"File embeddings non trovato! Esegui prima index_database().")
        data = np.load(self.emb_file)
        return data['embeddings']

    def search(self, query: str, emb_matrix: np.ndarray, top_k: int = 20) -> list[tuple[int, float]]:
        logger.debug(f"Inizio ricerca per query: '{query}' (top_k={top_k})")
        with torch.no_grad():
            q_emb = self.model.encode([query], convert_to_numpy=True, normalize_embeddings=True)[0]
        sims = np.dot(emb_matrix, q_emb)
        top_k = min(top_k, len(sims))
        idxs = np.argpartition(-sims, top_k)[:top_k]
        results = sorted([(int(i), float(sims[i])) for i in idxs], key=lambda x: x[1], reverse=True)
        return results

    def run(self, query: str, top_k: int = 5, visualize: bool = False) -> str:
        logger.info(f"Inizio esecuzione RAG - Query: '{query}', top_k: {top_k}, visualize: {visualize}")
        data = self._read_data()
        if self.reindex or not os.path.exists(self.emb_file):
            emb = self.index_database(data)
        else:
            emb = self.load_embedding_matrix()
            with torch.no_grad():
                test_emb = self.model.encode([data[0]], convert_to_numpy=True)[0]
            if test_emb.shape[0] != emb.shape[1]:
                logger.warning("Embedding dimension mismatch, rigenero database")
                emb = self.index_database(data)
        results = self.search(query, emb, top_k)
        return "; ".join(data[idx] for idx, _ in results)
