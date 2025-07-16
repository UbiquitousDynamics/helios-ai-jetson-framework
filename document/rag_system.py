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
    """
    Classe per indicizzare, cercare embeddings di testo usando SentenceTransformers.
    Legge tutti i file .txt da una directory e li combina.
    Ottimizzata per dispositivi con risorse limitate come Jetson Nano.

    Parametri:
        txt_dir (str): Percorso alla directory contenente i file .txt.
        emb_file (str): Percorso dove salvare/caricare la matrice di embeddings (.npz).
        model_name (str): Nome del modello da caricare (default: modello leggero).
        reindex (bool): Se rigenerare sempre gli embeddings.
    """
    def __init__(self,
                 txt_dir: str = "uploads",
                 emb_file: str = "embeddings.npz",
                 model_name: str = 'all-MiniLM-L6-v2',
                 reindex: bool = False):
        logger.debug(f"Inizializzazione RagSystem - txt_dir: {txt_dir}, emb_file: {emb_file}, model_name: {model_name}, reindex: {reindex}")
        start_time = time.time()
        
        self.txt_dir = txt_dir
        self.emb_file = emb_file
        self.reindex = reindex

        logger.debug("Configurazione ottimizzazioni per dispositivi low-end")
        
        # Forza l'uso della CPU e ottimizza le impostazioni
        os.environ['TOKENIZERS_PARALLELISM'] = 'false'
        if torch.cuda.is_available():
            logger.info("CUDA disponibile ma forziamo CPU per stabilita su Jetson Nano")
        
        logger.debug(f"Inizio caricamento modello: {model_name}")
        model_start = time.time()
        
        # Carica il modello con ottimizzazioni
        self.model = SentenceTransformer(model_name, device='cpu')  # Forza CPU
        
        # Ottimizzazioni aggiuntive
        self.model.eval()  # Modalita evaluation per performance migliori
        
        model_end = time.time()
        logger.info(f"Modello '{model_name}' caricato in {model_end - model_start:.2f} secondi")
        
        init_time = time.time() - start_time
        logger.debug(f"Inizializzazione completata in {init_time:.2f} secondi")

    def _read_data(self) -> list[str]:

        logger.debug(f"Inizio lettura directory: {self.txt_dir}")
        start_time = time.time()
        
        if not os.path.exists(self.txt_dir) or not os.path.isdir(self.txt_dir):
            logger.error(f"Directory '{self.txt_dir}' non trovata!")
            raise FileNotFoundError(f"Directory '{self.txt_dir}' non trovata!")
        
        all_text = []
        txt_files = [f for f in sorted(os.listdir(self.txt_dir)) if f.lower().endswith('.txt')]
        
        if not txt_files:
            logger.error(f"Nessun file .txt trovato in: {self.txt_dir}")
            raise FileNotFoundError(f"Nessun file .txt trovato in {self.txt_dir}")
        
        logger.debug(f"Trovati {len(txt_files)} file .txt: {txt_files}")
        
        for fname in txt_files:
            fpath = os.path.join(self.txt_dir, fname)
            logger.debug(f"Lettura file: {fpath}")
            with open(fpath, 'r', encoding='utf-8') as f:
                text = f.read()
                all_text.append(text)
                logger.debug(f"Letto file '{fname}': {len(text)} caratteri")
        
        # Unisce tutti i testi
        combined_text = '\n'.join(all_text)
        logger.debug(f"Testo combinato: {len(combined_text)} caratteri totali")
        
        # Divide in frasi usando regex
        sentences = re.split(r'(?<=[\.!?])\s+', combined_text)
        sentences = [s.strip() for s in sentences if s.strip()]
        
        read_time = time.time() - start_time
        logger.info(f"Lette {len(sentences)} frasi da {len(txt_files)} file in {read_time:.2f} secondi")
        logger.debug(f"Prime 3 frasi: {sentences[:3] if len(sentences) >= 3 else sentences}")
        
        return sentences

    def index_database(self, data: list[str] | None = None) -> np.ndarray:
        """Indicizza il database generando embeddings per tutte le frasi."""
        logger.debug("Inizio indicizzazione database")
        start_time = time.time()
        
        if data is None:
            logger.debug("Data e None, carico da directory")
            data = self._read_data()
        
        logger.info(f"Inizio encoding di {len(data)} frasi")
        encode_start = time.time()
        
        # Batch size molto pi  piccolo per Jetson Nano
        batch_size = 16  # Ridotto per dispositivi con poca memoria
        embeddings_list = []
        
        # Disabilita gradient computation per risparmiare memoria
        with torch.no_grad():
            for i in range(0, len(data), batch_size):
                batch_start = time.time()
                batch = data[i:i+batch_size]
                logger.debug(f"Processing batch {i//batch_size + 1}/{(len(data)-1)//batch_size + 1}: {len(batch)} frasi")
                
        # Encoding con parametri ottimizzati per velocita
                batch_embeddings = self.model.encode(
                    batch, 
                    convert_to_numpy=True,
                    show_progress_bar=False,  # Disabilita progress bar per performance
                    batch_size=batch_size,
                    normalize_embeddings=True  # Normalizza per performance migliori
                )
                embeddings_list.append(batch_embeddings)
                
                batch_time = time.time() - batch_start
                logger.debug(f"Batch completato in {batch_time:.2f} secondi")
                
                # Garbage collection periodico per liberare memoria
                if i % (batch_size * 4) == 0:
                    gc.collect()
        
        embeddings = np.vstack(embeddings_list)
        encode_time = time.time() - encode_start
        logger.info(f"Encoding completato in {encode_time:.2f} secondi")
        
        logger.debug(f"Inizio salvataggio embeddings in '{self.emb_file}'")
        save_start = time.time()
        
        # Salva con compressione per risparmiare spazio
        np.savez_compressed(self.emb_file, embeddings=embeddings)
        
        save_time = time.time() - save_start
        logger.debug(f"Embeddings salvati in {save_time:.2f} secondi")
        
        total_time = time.time() - start_time
        logger.info(f"Indicizzazione completata in {total_time:.2f} secondi (shape={embeddings.shape})")
        
        return embeddings

    def load_embedding_matrix(self) -> np.ndarray:
        """Carica la matrice di embeddings dal file."""
        logger.debug(f"Inizio caricamento embeddings da {self.emb_file}")
        start_time = time.time()
        
        if not os.path.exists(self.emb_file):
            logger.error(f"File embeddings non trovato: {self.emb_file}")
            raise FileNotFoundError(f"File embeddings non trovato! Esegui prima index_database().")
        
        data = np.load(self.emb_file)
        embeddings = data['embeddings']
        
        load_time = time.time() - start_time
        logger.info(f"Embeddings caricati in {load_time:.2f} secondi (shape={embeddings.shape})")
        
        return embeddings

    def search(self, query: str, emb_matrix: np.ndarray, top_k: int = 20) -> list[tuple[int, float]]:
        logger.debug(f"Inizio ricerca per query: '{query}' (top_k={top_k})")
        start_time = time.time()
        
        logger.debug("Encoding query")
        query_encode_start = time.time()
        
        with torch.no_grad():
            q_emb = self.model.encode(
                [query], 
                convert_to_numpy=True, 
                normalize_embeddings=True,
                show_progress_bar=False
            )[0]
            
        query_encode_time = time.time() - query_encode_start
        logger.debug(f"Query encoded in {query_encode_time:.2f} secondi (shape: {q_emb.shape})")
        
        if q_emb.shape[0] != emb_matrix.shape[1]:
            logger.error(f"Dimensione embedding query ({q_emb.shape[0]}) incompatibile con matrix ({emb_matrix.shape[1]})")
            raise ValueError(f"Dimensione embedding query ({q_emb.shape[0]}) incompatibile con matrix ({emb_matrix.shape[1]})")
        
        logger.debug("Calcolo similarita coseno")
        similarity_start = time.time()
        
        # Usa dot product per embeddings normalizzati (pi  veloce)
        sims = np.dot(emb_matrix, q_emb)
        
        similarity_time = time.time() - similarity_start
        logger.debug(f"Similarita calcolata in {similarity_time:.2f} secondi")
        
        logger.debug("Ordinamento risultati")
        sort_start = time.time()
        
        # Usa argpartition per top-k pi  efficiente
        top_k = min(top_k, len(sims))
        idxs = np.argpartition(-sims, top_k)[:top_k]
        results = sorted([(int(i), float(sims[i])) for i in idxs], key=lambda x: x[1], reverse=True)
        
        sort_time = time.time() - sort_start
        logger.debug(f"Risultati ordinati in {sort_time:.2f} secondi")
        
        total_time = time.time() - start_time
        logger.info(f"Ricerca completata in {total_time:.2f} secondi")
        logger.debug(f"Top 3 scores: {results[:3]}")
        
        return results

    def run(self, query: str, top_k: int = 5, visualize: bool = False) -> str:
        
        logger.info(f"Inizio esecuzione RAG - Query: '{query}', top_k: {top_k}, visualize: {visualize}")
        total_start = time.time()
        
        logger.debug("Caricamento dati")
        data = self._read_data()
        
        emb = None
        # Carica o rigenera embeddings
        if self.reindex or not os.path.exists(self.emb_file):
            logger.info("Rigenerazione embeddings richiesta o file non esistente")
            emb = self.index_database(data)
        else:
            logger.info("Caricamento embeddings esistenti")
            emb = self.load_embedding_matrix()
            
            # Verifica compatibilita dimensionale
            logger.debug("Verifica compatibilita dimensionale")
            compat_start = time.time()
            with torch.no_grad():
                query_emb = self.model.encode([data[0]], convert_to_numpy=True)[0]
            compat_time = time.time() - compat_start
            logger.debug(f"Test compatibilita completato in {compat_time:.2f} secondi")
            
            if query_emb.shape[0] != emb.shape[1]:
                logger.warning(f"Dimensione embedding cambiata ({query_emb.shape[0]} vs {emb.shape[1]}), rigenero database...")
                emb = self.index_database(data)

        # Ricerca
        logger.info("Inizio ricerca")
        results = self.search(query, emb, top_k)
        
        logger.info(f"Stampa risultati top-{top_k}")
        ret = ""
        for idx, score in results[:top_k]:
            ret += f"{data[idx]}; "
        # Visualizzazione opzionale
        if visualize:
            logger.info("Inizio visualizzazione")
            self.visualize_space_query(data, query, emb)
        
        total_time = time.time() - total_start
        logger.info(f"Esecuzione RAG completata in {total_time:.2f} secondi totali")
        
        return ret.strip()