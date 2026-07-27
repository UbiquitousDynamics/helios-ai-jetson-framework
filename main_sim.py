#!/usr/bin/env python3
"""
Test che simula esattamente quello che fa main.py per identificare il problema
"""
import sys
import os
import traceback

def test_main_simulation():
    """Simula esattamente il comportamento di main.py"""
    
    print("=== TEST SIMULAZIONE MAIN.PY ===")
    print(f"Working directory: {os.getcwd()}")
    print()
    
    # Test 1: Import dell'assistant come fa main.py
    print("1. Test import assistant (come in main.py)...")
    try:
        from assistant import VoiceAssistant
        print("   ✓ VoiceAssistant importato con successo")
    except Exception as e:
        print(f"   ✗ ERRORE import VoiceAssistant: {e}")
        print("\n=== STACK TRACE COMPLETO ===")
        traceback.print_exc()
        return False
    
    # Test 2: Creazione istanza VoiceAssistant
    print("\n2. Test creazione istanza VoiceAssistant...")
    try:
        # Prova senza documenti prima
        assistant = VoiceAssistant()
        print("   ✓ VoiceAssistant creato senza documenti")
    except Exception as e:
        print(f"   ✗ ERRORE creazione VoiceAssistant: {e}")
        traceback.print_exc()
        return False
    
    # Test 3: Creazione con documenti (se applicabile)
    print("\n3. Test creazione con documenti...")
    try:
        # Verifica se esistono documenti di test
        test_docs = {}
        if os.path.exists("uploads/regolamento.txt"):
            with open("uploads/regolamento.txt", 'r', encoding='utf-8') as f:
                test_docs = {"regolamento": f.read()[:1000]}  # Solo primi 1000 char per test
        
        assistant_with_docs = VoiceAssistant(documents=test_docs)
        print("   ✓ VoiceAssistant creato con documenti")
    except Exception as e:
        print(f"   ✗ ERRORE creazione VoiceAssistant con documenti: {e}")
        traceback.print_exc()
        return False
    
    print("\n🎉 SIMULAZIONE MAIN.PY COMPLETATA CON SUCCESSO!")
    return True

def test_individual_imports():
    """Test degli import individuali dell'assistant.py nell'ordine esatto"""
    
    print("\n=== TEST IMPORT INDIVIDUALI ASSISTANT.PY ===")
    
    imports_and_descriptions = [
        ("import logging", "Logging standard"),
        ("from multiprocessing import Process", "Multiprocessing"),
        ("from audio.tts import Pyttsx3TTS", "Text-to-Speech"),
        ("from audio.sound_player import SoundPlayer", "Sound Player"),
        ("from api.api_client import APIClient", "API Client"),
        ("from recognizer.speech_recognizer import SpeechRecognizer", "Speech Recognizer"),
        ("from document.document_retriever import DocumentRetriever", "Document Retriever"),
        ("import speech_recognition as sr", "Speech Recognition"),
        ("import config", "Config"),
        ("import random", "Random"),
        ("from document.rag_system import RagSystem", "RAG System (PyTorch)")
    ]
    
    for i, (import_statement, description) in enumerate(imports_and_descriptions, 1):
        print(f"{i:2d}. {description:<25} -> {import_statement}")
        try:
            exec(import_statement)
            print(f"     ✓ OK")
        except Exception as e:
            print(f"     ✗ ERRORE: {str(e)[:100]}...")
            print(f"\n=== DETTAGLI ERRORE IMPORT #{i} ===")
            print(f"Import fallito: {import_statement}")
            print(f"Descrizione: {description}")
            traceback.print_exc()
            return False, i, import_statement
    
    print("\n✅ TUTTI GLI IMPORT INDIVIDUALI OK!")
    return True, 0, ""

def check_environment():
    """Verifica l'ambiente e le dipendenze"""
    
    print("\n=== VERIFICA AMBIENTE ===")
    
    # Verifica file critici
    critical_files = [
        "assistant.py",
        "config.py", 
        "audio/tts.py",
        "audio/sound_player.py",
        "api/api_client.py",
        "recognizer/speech_recognizer.py",
        "document/document_retriever.py",
        "document/rag_system.py"
    ]
    
    missing_files = []
    for file in critical_files:
        if os.path.exists(file):
            print(f"✓ {file}")
        else:
            print(f"✗ {file} MANCANTE!")
            missing_files.append(file)
    
    if missing_files:
        print(f"\n❌ File mancanti: {missing_files}")
        return False
    
    # Verifica permessi
    for file in critical_files:
        if not os.access(file, os.R_OK):
            print(f"✗ {file} non leggibile!")
            return False
    
    print("\n✅ AMBIENTE OK!")
    return True

def main():
    """Esegue tutti i test in sequenza"""
    
    print("🔍 DIAGNOSI COMPLETA DEL PROBLEMA")
    print("=" * 50)
    
    # 1. Verifica ambiente
    if not check_environment():
        print("\n❌ PROBLEMA NELL'AMBIENTE - controlla i file mancanti")
        return
    
    # 2. Test import individuali
    success, failed_index, failed_import = test_individual_imports()
    if not success:
        print(f"\n❌ PROBLEMA ALL'IMPORT #{failed_index}: {failed_import}")
        print("Questo è il modulo che causa il conflitto con PyTorch!")
        return
    
    # 3. Test simulazione main
    if not test_main_simulation():
        print("\n❌ PROBLEMA NELLA SIMULAZIONE MAIN.PY")
        print("Il problema è nella creazione dell'istanza VoiceAssistant")
        return
    
    print("\n🎉 DIAGNOSI COMPLETATA - NESSUN PROBLEMA TROVATO!")
    print("Il problema potrebbe essere specifico al momento dell'esecuzione di main.py")
    print("Prova ad eseguire main.py con questo comando:")
    print("PYTHONPATH=/home/operator/Jetson-AI-Voice-Assistant python main.py")

if __name__ == "__main__":
    main()
