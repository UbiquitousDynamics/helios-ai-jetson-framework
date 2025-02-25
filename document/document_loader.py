import os
import logging
import pdfplumber
import config

class DocumentLoader:
    def __init__(self, folder: str = config.UPLOAD_FOLDER):
        self.folder = folder

    def is_allowed_file(self, filename: str) -> bool:
        # Check if the file extension is allowed
        result = '.' in filename and filename.rsplit('.', 1)[1].lower() in config.ALLOWED_EXTENSIONS
        logging.debug(f"File check for '{filename}': {'Allowed' if result else 'Not allowed'}.")
        return result

    def extract_text_from_pdf(self, pdf_path: str) -> str:
        logging.debug(f"Extracting text from PDF: {pdf_path}.")
        text = ""
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + "\n"
            logging.debug(f"Text successfully extracted from {pdf_path}.")
        except Exception as e:
            logging.error(f"Error extracting text from PDF '{pdf_path}': {e}")
        return text

    def load_documents(self) -> dict:
        documents = {}
        try:
            for filename in os.listdir(self.folder):
                file_path = os.path.join(self.folder, filename)
                if not self.is_allowed_file(filename):
                    continue
                if filename.lower().endswith(".pdf"):
                    documents[filename] = self.extract_text_from_pdf(file_path)
                elif filename.lower().endswith(".txt"):
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            documents[filename] = f.read()
                    except Exception as e:
                        logging.error(f"Error reading text file '{filename}': {e}")
                logging.info(f"Loaded document: {filename}")
        except Exception as e:
            logging.error(f"Error loading documents: {e}")
        return documents
