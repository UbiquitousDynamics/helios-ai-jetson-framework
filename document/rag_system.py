import os
import time
import logging
import numpy as np
from sklearn.manifold import TSNE
import matplotlib.pyplot as plt
from sklearn.metrics.pairwise import cosine_similarity
from sentence_transformers import SentenceTransformer
import torch

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
    Classe per indicizzare, cercare e visualizzare embeddings di testo usando SentenceTransformers.
    Ottimizzata per dispositivi con risorse limitate come Jetson Nano.

    Parametri:
        txt_file (str): Percorso al file di testo contenente le frasi, una per riga.
        emb_file (str): Percorso dove salvare/caricare la matrice di embeddings (.npy).
        model_name (str): Nome del modello da caricare (default: modello leggero).
        reindex (bool): Se rigenerare sempre gli embeddings.
    """
    def __init__(self,
                 txt_file: str,
                 emb_file: str = "embeddings.npy",
                 model_name: str = 'all-MiniLM-L6-v2',  # switch model (all-MiniLM-L6-v2, paraphrase-MiniLM-L3-v2, distiluse-base-multilinguage-cased, BAAI/bge-m3)
                 reindex: bool = False):
        logger.debug(f"Inizializzazione RagSystem - txt_file: {txt_file}, emb_file: {emb_file}, model_name: {model_name}, reindex: {reindex}")
        start_time = time.time()
        
        self.txt_file = txt_file
        self.emb_file = emb_file
        self.reindex = reindex

        # Ottimizzazioni per dispositivi con poca memoria
        logger.debug("Configurazione ottimizzazioni per dispositivi low-end")
        
        # Forza l'uso della CPU e ottimizza le impostazioni
        os.environ['TOKENIZERS_PARALLELISM'] = 'false'
        if torch.cuda.is_available():
            logger.info("CUDA disponibile ma forziamo CPU per stabilità su Jetson Nano")
        
        logger.debug(f"Inizio caricamento modello: {model_name}")
        model_start = time.time()
        
        # Carica il modello con ottimizzazioni
        self.model = SentenceTransformer(model_name, device='cpu')  # Forza CPU
        
        # Ottimizzazioni aggiuntive
        self.model.eval()  # Modalità evaluation per performance migliori
        
        model_end = time.time()
        logger.info(f"Modello '{model_name}' caricato in {model_end - model_start:.2f} secondi")
        
        init_time = time.time() - start_time
        logger.debug(f"Inizializzazione completata in {init_time:.2f} secondi")

    def _read_data(self) -> list[str]:
        logger.debug(f"Inizio lettura file: {self.txt_file}")
        start_time = time.time()
        
        if not os.path.exists(self.txt_file):
            logger.error(f"File '{self.txt_file}' non trovato!")
            raise FileNotFoundError(f"File '{self.txt_file}' non trovato!")
        
        logger.debug(f"File esistente, dimensione: {os.path.getsize(self.txt_file)} bytes")
        
        with open(self.txt_file, 'r', encoding='utf-8') as f:
            data = [line.strip() for line in f if line.strip()]
        
        read_time = time.time() - start_time
        logger.info(f"Letto {len(data)} frasi da '{self.txt_file}' in {read_time:.2f} secondi")
        logger.debug(f"Prime 3 frasi: {data[:3] if len(data) >= 3 else data}")
        
        return data

    def index_database(self, data: list[str] | None = None) -> np.ndarray:
        logger.debug("Inizio indicizzazione database")
        start_time = time.time()
        
        if data is None:
            logger.debug("Data è None, carico da file")
            data = self._read_data()
        
        logger.info(f"Inizio encoding di {len(data)} frasi")
        encode_start = time.time()
        
        # Batch size molto più piccolo per Jetson Nano
        batch_size = 16  # Ridotto da 100 a 16
        embeddings_list = []
        
        # Disabilita gradient computation per risparmiare memoria
        with torch.no_grad():
            for i in range(0, len(data), batch_size):
                batch_start = time.time()
                batch = data[i:i+batch_size]
                logger.debug(f"Processing batch {i//batch_size + 1}/{(len(data)-1)//batch_size + 1}: {len(batch)} frasi")
                
                # Encoding con parametri ottimizzati per velocità
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
                    import gc
                    gc.collect()
        
        embeddings = np.vstack(embeddings_list)
        encode_time = time.time() - encode_start
        logger.info(f"Encoding completato in {encode_time:.2f} secondi")
        
        logger.debug(f"Inizio salvataggio embeddings in '{self.emb_file}'")
        save_start = time.time()
        
        # Salva con compressione per risparmiare spazio
        np.savez_compressed(self.emb_file.replace('.npy', '.npz'), embeddings=embeddings)
        
        save_time = time.time() - save_start
        logger.debug(f"Embeddings salvati in {save_time:.2f} secondi")
        
        total_time = time.time() - start_time
        logger.info(f"Indicizzazione completata in {total_time:.2f} secondi (shape={embeddings.shape})")
        
        return embeddings

    def load_embedding_matrix(self) -> np.ndarray:
        logger.debug(f"Inizio caricamento embeddings")
        start_time = time.time()
        
        # Prova prima il formato compresso
        compressed_file = self.emb_file.replace('.npy', '.npz')
        
        if os.path.exists(compressed_file):
            logger.debug(f"Caricamento da file compresso: {compressed_file}")
            data = np.load(compressed_file)
            embeddings = data['embeddings']
        elif os.path.exists(self.emb_file):
            logger.debug(f"Caricamento da file non compresso: {self.emb_file}")
            embeddings = np.load(self.emb_file)
        else:
            logger.error(f"Nessun file di embeddings trovato!")
            raise FileNotFoundError(f"File embeddings non trovato! Esegui prima index_database().")
        
        load_time = time.time() - start_time
        logger.info(f"Embeddings caricati in {load_time:.2f} secondi (shape={embeddings.shape})")
        
        return embeddings

    def search(self, query: str, embedding_matrix: np.ndarray) -> list[tuple[int, float]]:
        logger.debug(f"Inizio ricerca per query: '{query}'")
        start_time = time.time()
        
        logger.debug("Encoding query")
        query_encode_start = time.time()
        
        with torch.no_grad():  # Disabilita gradient per performance
            query_emb = self.model.encode(
                [query], 
                convert_to_numpy=True,
                show_progress_bar=False,
                normalize_embeddings=True
            )[0]
            
        query_encode_time = time.time() - query_encode_start
        logger.debug(f"Query encoded in {query_encode_time:.2f} secondi (shape: {query_emb.shape})")
        
        if query_emb.shape[0] != embedding_matrix.shape[1]:
            logger.error(f"Dimensione embedding query ({query_emb.shape[0]}) incompatibile con matrix ({embedding_matrix.shape[1]})")
            raise ValueError(f"Dimensione embedding query ({query_emb.shape[0]}) incompatibile con matrix ({embedding_matrix.shape[1]})")
        
        logger.debug("Calcolo similarità coseno")
        similarity_start = time.time()
        
        # Usa dot product invece di cosine_similarity per embeddings normalizzati (più veloce)
        if hasattr(embedding_matrix, 'dtype') and embedding_matrix.dtype == np.float32:
            sims = np.dot(embedding_matrix, query_emb.astype(np.float32))
        else:
            sims = cosine_similarity([query_emb], embedding_matrix)[0]
            
        similarity_time = time.time() - similarity_start
        logger.debug(f"Similarità calcolata in {similarity_time:.2f} secondi")
        
        logger.debug("Ordinamento risultati")
        sort_start = time.time()
        
        # Usa argpartition per top-k più efficiente se ci sono molti documenti
        if len(sims) > 100:
            top_indices = np.argpartition(sims, -min(50, len(sims)))[-min(50, len(sims)):]
            results = [(idx, sims[idx]) for idx in top_indices]
            results.sort(key=lambda x: x[1], reverse=True)
        else:
            results = sorted(enumerate(sims), key=lambda x: x[1], reverse=True)
            
        sort_time = time.time() - sort_start
        logger.debug(f"Risultati ordinati in {sort_time:.2f} secondi")
        
        total_time = time.time() - start_time
        logger.info(f"Ricerca completata in {total_time:.2f} secondi")
        logger.debug(f"Top 3 scores: {[(idx, score) for idx, score in results[:3]]}")
        
        return results

    def visualize_space_query(self,
                              data: list[str],
                              query: str,
                              embedding_matrix: np.ndarray,
                              perplexity: int = 2,
                              random_state: int = 42) -> None:
        logger.debug("Inizio visualizzazione t-SNE")
        start_time = time.time()
        
        logger.debug("Encoding query per visualizzazione")
        with torch.no_grad():
            query_emb = self.model.encode(
                [query], 
                convert_to_numpy=True,
                show_progress_bar=False,
                normalize_embeddings=True
            )[0]
        
        if query_emb.shape[0] != embedding_matrix.shape[1]:
            logger.error("Dimensione embedding mismatch per t-SNE visualizzazione")
            raise ValueError("Dimensione embedding mismatch per t-SNE visualizzazione.")
        
        logger.debug("Unione embeddings per t-SNE")
        joint = np.vstack([embedding_matrix, query_emb])
        logger.debug(f"Matrice congiunta shape: {joint.shape}")
        
        logger.debug(f"Inizio t-SNE con perplexity={perplexity}")
        tsne_start = time.time()
        
        # Parametri t-SNE ottimizzati per performance
        tsne = TSNE(
            n_components=2, 
            perplexity=min(perplexity, (len(joint)-1)//3),  # Evita perplexity troppo alto
            random_state=random_state,
            n_iter=250,  # Ridotto da default 1000
            learning_rate='auto'
        )
        emb2d = tsne.fit_transform(joint.astype(np.float64))  # Assicura tipo corretto
        
        tsne_time = time.time() - tsne_start
        logger.info(f"t-SNE completato in {tsne_time:.2f} secondi")
        
        logger.debug("Creazione plot")
        plot_start = time.time()
        plt.figure(figsize=(8, 6))
        plt.scatter(emb2d[:-1, 0], emb2d[:-1, 1], edgecolor='k', label='Frasi')
        plt.scatter(emb2d[-1, 0], emb2d[-1, 1], edgecolor='k', label='Query', c='red')
        
        # Mostra solo le prime N frasi per evitare sovraffollamento
        max_labels = min(20, len(data))
        for i in range(max_labels):
            frase = data[i][:50] + "..." if len(data[i]) > 50 else data[i]  # Tronca frasi lunghe
            plt.text(emb2d[i, 0] + 0.1, emb2d[i, 1] + 0.1, frase, fontsize=8)
            
        query_short = query[:50] + "..." if len(query) > 50 else query
        plt.text(emb2d[-1, 0] + 0.1, emb2d[-1, 1] + 0.1, query_short, fontsize=8, color='red')
        
        plt.title('Visualizzazione degli Embeddings con t-SNE')
        plt.xlabel('Dimensione 1')
        plt.ylabel('Dimensione 2')
        plt.grid(True)
        plt.legend()
        plt.tight_layout()
        plt.show()
        
        plot_time = time.time() - plot_start
        logger.debug(f"Plot creato in {plot_time:.2f} secondi")
        
        total_time = time.time() - start_time
        logger.info(f"Visualizzazione completata in {total_time:.2f} secondi")

    def run(self, query: str, top_k: int = 5, visualize: bool = False) -> None:
        logger.info(f"Inizio esecuzione RAG - Query: '{query}', top_k: {top_k}, visualize: {visualize}")
        total_start = time.time()
        
        logger.debug("Caricamento dati")
        data = self._read_data()
        
        emb = None
        # Carica o rigenera embeddings
        if self.reindex or not (os.path.exists(self.emb_file) or os.path.exists(self.emb_file.replace('.npy', '.npz'))):
            logger.info("Rigenerazione embeddings richiesta o file non esistente")
            emb = self.index_database(data)
        else:
            logger.info("Caricamento embeddings esistenti")
            emb = self.load_embedding_matrix()
            
            # Verifica compatibilità dimensionale
            logger.debug("Verifica compatibilità dimensionale")
            compat_start = time.time()
            with torch.no_grad():
                query_emb = self.model.encode([data[0]], convert_to_numpy=True)[0]
            compat_time = time.time() - compat_start
            logger.debug(f"Test compatibilità completato in {compat_time:.2f} secondi")
            
            if query_emb.shape[0] != emb.shape[1]:
                logger.warning(f"Dimensione embedding cambiata ({query_emb.shape[0]} vs {emb.shape[1]}), rigenero database...")
                emb = self.index_database(data)

        # Ricerca
        logger.info("Inizio ricerca")
        results = self.search(query, emb)
        
        logger.info(f"Stampa risultati top-{top_k}")
        print(f"\nTop-{top_k} frasi più simili a '{query}':")
        for idx, score in results[:top_k]:
            print(f"  [{idx}] (score={score:.4f}): {data[idx]}")

        # Visualizzazione
        if visualize:
            logger.info("Inizio visualizzazione")
            self.visualize_space_query(data, query, emb)
        
        total_time = time.time() - total_start
        logger.info(f"Esecuzione RAG completata in {total_time:.2f} secondi totali")


def main():
    logger.info("Inizio programma principale")
    main_start = time.time()
    
    searcher = RagSystem(
        txt_file="uploads/regolamento.txt",
        emb_file="embeddings.npy",
        model_name='all-MiniLM-L6-v2',
        reindex=False
    )
    searcher.run(query="How mny liters of water?", top_k=5, visualize=False)
    
    main_time = time.time() - main_start
    logger.info(f"Programma principale completato in {main_time:.2f} secondi")

if __name__ == "__main__":
    main()