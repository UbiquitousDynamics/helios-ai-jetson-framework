import logging
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

class DocumentRetriever:
    def __init__(self, documents: dict):
        self.documents = documents
        try:
            # Initialize TF-IDF Vectorizer and compute document matrix
            self.tfidf_vectorizer = TfidfVectorizer(stop_words=None)
            self.doc_matrix = self.tfidf_vectorizer.fit_transform(documents.values())
            logging.debug("DocumentRetriever initialized successfully.")
        except Exception as e:
            logging.error(f"Error initializing DocumentRetriever: {e}")

    def retrieve(self, query: str, top_k: int = 2):
        try:
            # Transform query and compute cosine similarity with document matrix
            query_vector = self.tfidf_vectorizer.transform([query])
            similarity_scores = cosine_similarity(query_vector, self.doc_matrix).flatten()
            top_indices = similarity_scores.argsort()[-top_k:][::-1]
            top_documents = [(list(self.documents.keys())[i], list(self.documents.values())[i]) for i in top_indices]
            logging.info(f"Documents retrieved: {[doc[0] for doc in top_documents]}.")
            return top_documents
        except Exception as e:
            logging.error(f"Error during document retrieval: {e}")
            return []
