import logging
from multiprocessing import Process, Manager
from document.document_loader import DocumentLoader
from assistant import VoiceAssistant

def load_documents(shared_documents):
    """
    Load documents using DocumentLoader and update the shared dictionary.
    """
    doc_loader = DocumentLoader()
    docs = doc_loader.load_documents()
    shared_documents.update(docs)

if __name__ == '__main__':
    # Configure logging to output debug information
    #logging.basicConfig(level=logging.DEBUG)
    logging.disable(logging.CRITICAL)
    # Create a shared dictionary for the documents using a Manager
    manager = Manager()
    shared_documents = manager.dict()
    
    # Start a separate process for loading documents
    doc_process = Process(target=load_documents, args=(shared_documents,))
    doc_process.start()
    doc_process.join()  # Wait for the document loading process to complete
    
    logging.info("Documents loaded.")
    
    # Convert the shared manager dictionary to a normal dictionary
    documents = dict(shared_documents)
    
    # Initialize and run the voice assistant with the loaded documents
    assistant = VoiceAssistant(documents=documents)
    assistant.run()
