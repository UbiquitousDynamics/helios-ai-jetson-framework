import os
import numpy as np
from FlagEmbedding import BGEM3FlagModel
from sklearn.manifold import TSNE
#import matplotlib.pyplot as plt
from sklearn.metrics.pairwise import cosine_similarity
import logging

class RagSystem:
    def __init__(self, model_name='BAAI/bge-base-en-v1.5', use_fp16=True):
        """
        Inizializza il sistema RAG con un modello di embedding.
        
        Args:
            model_name: Nome del modello di embedding da utilizzare
            use_fp16: Se utilizzare la precisione fp16 per velocizzare il calcolo
        """
        try:
            self.model = BGEM3FlagModel(model_name, use_fp16=use_fp16)
            self.embedding_matrix = None
            self.data = None  # Original sentences
            logging.info(f"Inizializzato RagSystem con modello {model_name}")
        except Exception as e:
            logging.error(f"Errore nell'inizializzazione del modello: {e}")
            raise

    def index_database(self, data, save_path='embeddings.npy', text_data_path='sentences.txt'):
        """
        Codifica i dati di input e salva gli embedding solo se non esistono già.
        Salva anche le frasi originali per il confronto durante il ricaricamento.
        
        Args:
            data: Lista di stringhe da codificare
            save_path: Percorso dove salvare gli embedding
            text_data_path: Percorso dove salvare i testi originali
        """
        if not data:
            raise ValueError("Dati di input vuoti. Impossibile creare indice.")
            
        self.data = data

        # Controlla se gli embeddings esistono già
        if os.path.exists(save_path) and os.path.exists(text_data_path):
            try:
                with open(text_data_path, 'r', encoding='utf-8') as f:
                    saved_sentences = [line.strip() for line in f.readlines()]

                # Se le frasi sono le stesse, ricarica gli embeddings
                if saved_sentences == data:
                    logging.info("Found existing embeddings. Loading them from disk.")
                    self.embedding_matrix = np.load(save_path)
                    return
            except Exception as e:
                logging.warning(f"Errore nel caricamento degli embeddings esistenti: {e}")

        logging.info(f"Computing new embeddings for {len(data)} items and saving them.")
        try:
            embeddings = self.model.encode(data)['dense_vecs']
            self.embedding_matrix = np.array(embeddings)  # Assicuriamoci che sia un array numpy

            # Salva gli embeddings
            np.save(save_path, self.embedding_matrix)

            # Salva anche le frasi per il confronto futuro
            with open(text_data_path, 'w', encoding='utf-8') as f:
                f.write("\n".join(data))
        except Exception as e:
            logging.error(f"Errore nel calcolo degli embeddings: {e}")
            raise

    def load_embedding_matrix(self, embeddings_path, text_data_path='sentences.txt'):
        """
        Carica gli embedding da file.
        
        Args:
            embeddings_path: Percorso degli embedding salvati
            text_data_path: Percorso dei testi originali
        
        Returns:
            La matrice degli embedding caricata
        """
        try:
            self.embedding_matrix = np.load(embeddings_path)
            
            # Carica anche i dati originali se disponibili
            if os.path.exists(text_data_path):
                with open(text_data_path, 'r', encoding='utf-8') as f:
                    self.data = [line.strip() for line in f.readlines()]
                logging.info(f"Caricati {len(self.data)} testi originali")
            else:
                logging.warning("File dei testi originali non trovato")
                
            return self.embedding_matrix
        except Exception as e:
            logging.error(f"Errore nel caricamento degli embeddings: {e}")
            raise

    def search(self, query, top_k=1):
        """
        Cerca i documenti più simili alla query.
        
        Args:
            query: Stringa di ricerca
            top_k: Numero di risultati da restituire
            
        Returns:
            Lista di tuple (indice, punteggio di similarità)
        """
        if self.embedding_matrix is None:
            raise ValueError("Embedding matrix not loaded. Run index_database() or load_embedding_matrix() first.")

        try:
            query_embedding = self.model.encode([query])['dense_vecs']
            similarities = cosine_similarity(query_embedding, self.embedding_matrix)[0]
            
            # Ordina per similarità in ordine decrescente
            similarities_results = sorted(enumerate(similarities), key=lambda x: x[1], reverse=True)
            
            # Limita i risultati se è stato specificato top_k
            if top_k:
               similarities_results = similarities_results[:top_k]
   
            logging.debug("\nSimilarity with the query (in descending order):\n")
            results = []
            for idx, similarity in similarities_results:
                result_text = self.data[idx] if self.data else "N/A"
                logging.debug(f"Index: {idx} | Similarity: {similarity:.4f} | Result: {result_text}")
                results.append((idx, similarity, result_text))

            return results
        except Exception as e:
            logging.error(f"Errore nella ricerca: {e}")
            raise


   # def visualize_space_query(self, query, perplexity=5, figure_size=(10, 8), save_path=None):
   #     """
   #     Visualizza lo spazio degli embedding con t-SNE, evidenziando la query.
   #     
   #     Args:
   #         query: Stringa di ricerca da visualizzare nello spazio
   #         perplexity: Parametro di perplexity per t-SNE
   #         figure_size: Dimensione della figura in pollici
   #         save_path: Se specificato, salva la figura in questo percorso
   #     """
   #     if self.embedding_matrix is None or self.data is None:
   #         raise ValueError("You must first run index_database() to have both data and embeddings.")
#
   #     try:
   #         # Codifica la query
   #         query_embedding = self.model.encode([query])['dense_vecs']
   #         
   #         # Unisci gli embedding esistenti con quello della query
   #         jointed_matrix = np.vstack((self.embedding_matrix, query_embedding))
#
   #         # Calcola t-SNE
   #         tsne = TSNE(n_components=2, perplexity=perplexity, random_state=42, n_iter=1000)
   #         embeddings_2d = tsne.fit_transform(jointed_matrix)
#
   #         # Visualizza
   #         plt.figure(figsize=figure_size)
   #         plt.scatter(embeddings_2d[:-1, 0], embeddings_2d[:-1, 1], color='blue', alpha=0.7, 
   #                     edgecolors='k', label='Sentences')
   #         plt.scatter(embeddings_2d[-1, 0], embeddings_2d[-1, 1], color='red', s=100, 
   #                     edgecolors='k', label='Query')
#
   #         # Limita il numero di etichette per evitare sovraccarico visivo
   #         max_labels = min(20, len(self.data))
   #         step = len(self.data) // max_labels if len(self.data) > max_labels else 1
   #         
   #         for i in range(0, len(self.data), step):
   #             # Abbrevia le stringhe troppo lunghe per la visualizzazione
   #             short_text = self.data[i][:50] + '...' if len(self.data[i]) > 50 else self.data[i]
   #             plt.text(embeddings_2d[i, 0] + 0.05, embeddings_2d[i, 1] + 0.05, 
   #                      short_text, fontsize=8)
#
   #         # Aggiungi etichetta per la query
   #         short_query = query[:50] + '...' if len(query) > 50 else query
   #         plt.text(embeddings_2d[-1, 0] + 0.05, embeddings_2d[-1, 1] + 0.05, 
   #                  f"Query: {short_query}", fontsize=9, color='red', weight='bold')
   #         
   #         plt.title("t-SNE Visualization of Sentence Embeddings with Query")
   #         plt.xlabel("t-SNE Component 1")
   #         plt.ylabel("t-SNE Component 2")
   #         plt.legend()
   #         plt.grid(True)
   #         plt.tight_layout()
   #         
   #         if save_path:
   #             plt.savefig(save_path, dpi=300, bbox_inches='tight')
   #             logging.info(f"Figura salvata in {save_path}")
   #             
   #         plt.show()
   #         
   #     except Exception as e:
   #         logging.error(f"Errore nella visualizzazione: {e}")
   #         raise