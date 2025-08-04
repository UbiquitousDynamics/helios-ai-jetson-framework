from multiprocessing import Manager
from document.document_loader import DocumentLoader
from assistant import VoiceAssistant
import logging


def load_documents(shared_documents):
    """
    Load documents using DocumentLoader and update the shared dictionary.
    """
    doc_loader = DocumentLoader()
    docs = doc_loader.load_documents()
    shared_documents.update(docs)

if __name__ == '__main__':
    # Configure logging using config.py parameters
    # Disabilitato logging
    logging.disable(logging.CRITICAL)
    # if LOG_FILE:
    #     logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT, filename=LOG_FILE, filemode='a')
    # else:
    #     logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    # Create a shared dictionary for the documents using a Manager
    manager = Manager()
    shared_documents = manager.dict()
    
    # Start a separate process for loading documents
    #doc_process = Process(target=load_documents, args=(shared_documents,))
    #doc_process.start()
   # doc_process.join()  # Wait for the document loading process to complete
    
   # logging.info("Documents loaded.")
    # Convert the shared manager dictionary to a normal dictionary
    #documents = dict(shared_documents)
    
    # Initialize and run the voice assistant with the loaded documents
    assistant = VoiceAssistant()
    assistant.run()
