import os
from sentence_transformers import SentenceTransformer

# Percorso di destinazione
local_model_path = "models/all-MiniLM-L6-v2"

# Crea la cartella se non esiste
os.makedirs(local_model_path, exist_ok=True)

# Scarica e salva il modello localmente
print(f"Downloading and saving model to: {local_model_path}")
model = SentenceTransformer('all-MiniLM-L6-v2')
model.save(local_model_path)

print("Modello scaricato e salvato correttamente.")
