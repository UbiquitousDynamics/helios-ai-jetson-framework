#!/usr/bin/env python3
import sys
import os
import traceback

def test_imports():
    """Test degli import passo-passo per identificare il punto di errore"""
    
    print("=== TEST IMPORT DIAGNOSTICO ===")
    print(f"Working directory: {os.getcwd()}")
    print(f"Python version: {sys.version}")
    print()
    
    # Test 1: Import base
    print("1. Test import base...")
    try:
        import torch
        print("   ✓ torch importato con successo")
        print(f"   - Versione torch: {torch.__version__}")
        print(f"   - CUDA disponibile: {torch.cuda.is_available()}")
    except Exception as e:
        print(f"   ✗ Errore import torch: {e}")
        traceback.print_exc()
        return False
    
    # Test 2: Import sentence-transformers
    print("\n2. Test import sentence-transformers...")
    try:
        from sentence_transformers import SentenceTransformer
        print("   ✓ sentence-transformers importato con successo")
    except Exception as e:
        print(f"   ✗ Errore import sentence-transformers: {e}")
        traceback.print_exc()
        return False
    
    # Test 3: Test creazione modello
    print("\n3. Test creazione modello SentenceTransformer...")
    try:
        model = SentenceTransformer('all-MiniLM-L6-v2', device='cpu')
        print("   ✓ Modello creato con successo")
    except Exception as e:
        print(f"   ✗ Errore creazione modello: {e}")
        traceback.print_exc()
        return False
    
    # Test 4: Import del modulo rag_system
    print("\n4. Test import rag_system...")
    try:
        from document.rag_system import RagSystem
        print("   ✓ RagSystem importato con successo")
    except Exception as e:
        print(f"   ✗ Errore import RagSystem: {e}")
        traceback.print_exc()
        return False
    
    # Test 5: Creazione istanza RagSystem
    print("\n5. Test creazione istanza RagSystem...")
    try:
        rag = RagSystem(
            txt_file="uploads/regolamento.txt",
            emb_file="embeddings.npy",
            model_name='all-MiniLM-L6-v2',
            reindex=False
        )
        print("   ✓ Istanza RagSystem creata con successo")
    except Exception as e:
        print(f"   ✗ Errore creazione RagSystem: {e}")
        traceback.print_exc()
        return False
    
    print("\n=== TUTTI I TEST SUPERATI! ===")
    return True

def test_import_order():
    """Test dell'ordine di import come nel file assistant.py originale"""
    
    print("\n=== TEST ORDINE IMPORT (come in assistant.py) ===")
    
    imports_to_test = [
        "import logging",
        "from multiprocessing import Process",
        "from audio.tts import Pyttsx3TTS",
        "from audio.sound_player import SoundPlayer", 
        "from api.api_client import APIClient",
        "from recognizer.speech_recognizer import SpeechRecognizer",
        "from document.document_retriever import DocumentRetriever",
        "import speech_recognition as sr",
        "import config",
        "import random",
        "from document.rag_system import RagSystem"
    ]
    
    for i, import_statement in enumerate(imports_to_test, 1):
        print(f"{i:2d}. {import_statement}")
        try:
            exec(import_statement)
            print(f"    ✓ Successo")
        except Exception as e:
            print(f"    ✗ ERRORE: {e}")
            print(f"    Fallito all'import: {import_statement}")
            traceback.print_exc()
            return False
    
    print("\n=== TUTTI GLI IMPORT DELL'ASSISTANT SUPERATI! ===")
    return True

def main():
    """Esegue tutti i test diagnostici"""
    
    success = True
    
    # Esegui test base
    if not test_imports():
        success = False
        print("\n❌ Test base falliti - il problema è nell'import di PyTorch/SentenceTransformers")
    
    # Esegui test ordine import
    if success and not test_import_order():
        success = False
        print("\n❌ Test ordine import falliti - il problema è nell'ordine degli import")
    
    if success:
        print("\n🎉 TUTTI I TEST SUPERATI!")
        print("Il problema potrebbe essere in un'altra parte del codice.")
    else:
        print("\n💥 PROBLEMA IDENTIFICATO!")
        print("Controlla gli errori sopra riportati.")

if __name__ == "__main__":
    main()
