"""
FastAPI implementation for Podcastify podcast generation service.

This module provides REST endpoints for podcast generation with automatic
storage posting and callback notification.
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks
import os
import yaml
from typing import Dict, Any
from pathlib import Path
from ..client import generate_podcast
import uvicorn
import httpx


def load_base_config() -> Dict[Any, Any]:
    config_path = Path(__file__).parent / "podcastfy" / "conversation_config.yaml"
    try:
        with open(config_path, 'r') as file:
            return yaml.safe_load(file)
    except Exception as e:
        print(f"Warning: Could not load base config: {e}")
        return {}

def merge_configs(base_config: Dict[Any, Any], user_config: Dict[Any, Any]) -> Dict[Any, Any]:
    """Merge user configuration with base configuration, preferring user values."""
    merged = base_config.copy()
    
    # Handle special cases for nested dictionaries
    if 'text_to_speech' in merged and 'text_to_speech' in user_config:
        merged['text_to_speech'].update(user_config.get('text_to_speech', {}))
    
    # Update top-level keys
    for key, value in user_config.items():
        if key != 'text_to_speech':  # Skip text_to_speech as it's handled above
            if value is not None:  # Only update if value is not None
                merged[key] = value
                
    return merged

app = FastAPI()

TEMP_DIR = os.path.join(os.path.dirname(__file__), "temp_audio")
os.makedirs(TEMP_DIR, exist_ok=True)

async def generate_podcast_background(data: dict):
    """Background task to generate podcast and post to storage"""
    audio_file_path = None
    try:
        # Set environment variables
        if data.get('openai_key'):
            os.environ['OPENAI_API_KEY'] = data.get('openai_key')
        if data.get('google_key'):
            os.environ['GEMINI_API_KEY'] = data.get('google_key')
        if data.get('elevenlabs_key'):
            os.environ['ELEVENLABS_API_KEY'] = data.get('elevenlabs_key')

        # Load base configuration
        base_config = load_base_config()
        
        # Get TTS model and its configuration from base config
        tts_model = data.get('tts_model', base_config.get('text_to_speech', {}).get('default_tts_model', 'openai'))
        tts_base_config = base_config.get('text_to_speech', {}).get(tts_model, {})
        
        # Get voices (use user-provided voices or fall back to defaults)
        voices = data.get('voices', {})
        default_voices = tts_base_config.get('default_voices', {})
        
        # Prepare user configuration
        user_config = {
            'creativity': float(data.get('creativity', base_config.get('creativity', 0.7))),
            'conversation_style': data.get('conversation_style', base_config.get('conversation_style', [])),
            'roles_person1': data.get('roles_person1', base_config.get('roles_person1')),
            'roles_person2': data.get('roles_person2', base_config.get('roles_person2')),
            'dialogue_structure': data.get('dialogue_structure', base_config.get('dialogue_structure', [])),
            'podcast_name': data.get('name', base_config.get('podcast_name')),
            'podcast_tagline': data.get('tagline', base_config.get('podcast_tagline')),
            'output_language': data.get('output_language', base_config.get('output_language', 'English')),
            'user_instructions': data.get('user_instructions', base_config.get('user_instructions', '')),
            'engagement_techniques': data.get('engagement_techniques', base_config.get('engagement_techniques', [])),
            'text_to_speech': {
                'default_tts_model': tts_model,
                'model': tts_base_config.get('model'),
                'default_voices': {
                    'question': voices.get('question', default_voices.get('question')),
                    'answer': voices.get('answer', default_voices.get('answer'))
                }
            }
        }

        # Merge configurations
        conversation_config = merge_configs(base_config, user_config)

        # Generate podcast
        result = generate_podcast(
            text=data.get('text'),
            conversation_config=conversation_config,
            tts_model=tts_model,
            longform=bool(data.get('is_long_form', False)),
        )
        
        # Get the audio file path
        if isinstance(result, str) and os.path.isfile(result):
            audio_file_path = result
        elif hasattr(result, 'audio_path'):
            audio_file_path = result.audio_path
        else:
            print(f"Error: Invalid result from generate_podcast: {result}")
            return
        
        # Post audio file to uploadURL
        upload_url = data.get('uploadURL')
        if not upload_url:
            print("Error: uploadURL is required")
            return
        
        async with httpx.AsyncClient(timeout=300.0) as client:
            # Read audio file and post it
            with open(audio_file_path, 'rb') as audio_file:
                filename = os.path.basename(audio_file_path)
                files = {'file': (filename, audio_file.read(), 'audio/mpeg')}
                response = await client.post(upload_url, files=files)
                
                if response.status_code not in (200, 201):
                    print(f"Error posting to storage: {response.status_code} - {response.text}")
                    return
                
                # Extract storageId from response
                try:
                    response_data = response.json()
                    storage_id = response_data.get('storageId')
                    if not storage_id:
                        print(f"Error: storageId not found in response: {response_data}")
                        return
                except Exception as e:
                    print(f"Error parsing storage response: {e}")
                    return
        
        # Post storageId and userId to updateURL
        update_url = data.get('updateURL')
        if not update_url:
            print("Error: updateURL is required")
            return
        
        user_id = data.get('userId')
        if not user_id:
            print("Error: userId is required")
            return
        
        # Get Bearer token from environment variable
        api_key = os.getenv('UPDATE_URL_API_KEY')
        if not api_key:
            print("Error: UPDATE_URL_API_KEY environment variable not set")
            return
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {'Authorization': f'Bearer {api_key}'}
            update_response = await client.post(
                update_url,
                json={
                    'storageId': storage_id,
                    'userId': user_id
                },
                headers=headers
            )
            
            if update_response.status_code not in (200, 201):
                print(f"Error posting to updateURL: {update_response.status_code} - {update_response.text}")
            else:
                print(f"Successfully posted storageId {storage_id} for userId {user_id} to updateURL")
        
    except Exception as e:
        print(f"Error generating podcast: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Clean up temporary audio file if it exists
        if audio_file_path and os.path.exists(audio_file_path):
            try:
                # Only delete if it's in our temp directory
                if TEMP_DIR in audio_file_path:
                    os.remove(audio_file_path)
            except Exception as e:
                print(f"Error cleaning up audio file: {e}")

@app.post("/generate")
async def generate_podcast_endpoint(data: dict, background_tasks: BackgroundTasks):
    """
    Generate podcast asynchronously, post to storage, and notify update URL.
    
    Required fields:
    - userId: User identifier
    - uploadURL: URL to post the generated audio file to
    - updateURL: URL to post storageId and userId after successful upload (requires Bearer token from UPDATE_URL_API_KEY env var)
    - text: Text content to generate podcast from
    
    Optional fields:
    - openai_key, google_key, elevenlabs_key: API keys for TTS services
    - tts_model: TTS model to use
    - Other podcast configuration options
    """
    try:
        # Validate required fields
        if not data.get('userId'):
            raise HTTPException(status_code=400, detail="userId is required")
        if not data.get('uploadURL'):
            raise HTTPException(status_code=400, detail="uploadURL is required")
        if not data.get('updateURL'):
            raise HTTPException(status_code=400, detail="updateURL is required")
        if not data.get('text'):
            raise HTTPException(status_code=400, detail="text is required")
        
        # Add background task
        background_tasks.add_task(generate_podcast_background, data)
        
        # Return immediately
        return {"status": "accepted", "message": "Podcast generation started"}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def healthcheck():
    return {"status": "healthy"}

if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host=host, port=port)
