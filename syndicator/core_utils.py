from PIL import Image
import io
from django.core.files.base import ContentFile

def optimize_image_field(model_instance, field_name, max_size=(1200, 1200), quality=85):
    """
    Utility function to dynamically optimize and convert an image field to WebP format.
    It checks if the image has changed, scales it down using Lanczos resampling,
    compresses it to WebP format at the specified quality, and saves it.
    """
    field = getattr(model_instance, field_name)
    if not field:
        return
    
    has_changed = False
    if not model_instance.pk:
        has_changed = True
    else:
        try:
            # Query the database for the unmodified instance
            old_instance = model_instance.__class__.objects.get(pk=model_instance.pk)
            old_field = getattr(old_instance, field_name)
            if old_field != field:
                has_changed = True
        except model_instance.__class__.DoesNotExist:
            has_changed = True
            
    if has_changed and hasattr(field, 'file'):
        try:
            # Open the image file via Pillow
            img = Image.open(field)
            
            # Safe compatibility check for PIL resampling options
            try:
                resampling = Image.Resampling.LANCZOS
            except AttributeError:
                resampling = Image.ANTIALIAS

            # Standardize and handle image transparency/mode
            if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                img = img.convert('RGBA')
            else:
                img = img.convert('RGB')
                
            # Perform aspect-ratio preserving scaling
            img.thumbnail(max_size, resampling)
            
            # Save compressed bytes to an in-memory buffer
            output = io.BytesIO()
            img.save(output, format='WEBP', quality=quality)
            output.seek(0)
            
            # Determine the new file name with .webp extension
            original_name = field.name
            if '.' in original_name:
                base_name = original_name.rsplit('.', 1)[0]
            else:
                base_name = original_name
                
            new_filename = f"{base_name}.webp"
            
            # Save the file content into the field without invoking database save yet
            field.save(new_filename, ContentFile(output.read()), save=False)
        except Exception as e:
            # Log error or gracefully ignore processing failures to avoid breaking model saves
            print(f"Error optimizing image field '{field_name}' in model '{model_instance.__class__.__name__}': {e}")


def is_html_empty(html_value):
    """
    Checks if an HTML string (e.g. from CKEditor) is effectively empty.
    Handles cases like None, empty string, or whitespace-only HTML like <p>&nbsp;</p>.
    """
    if not html_value:
        return True
    from bs4 import BeautifulSoup
    text = BeautifulSoup(html_value, 'html.parser').get_text(strip=True)
    # Strip non-breaking spaces and whitespace
    text = text.replace('\u00a0', '').strip()
    return len(text) == 0


def translate_text(text, source='ar', target='en'):
    """
    Translates text from `source` language to `target` language using Google Translate
    via deep-translator. Handles both plain text and HTML content.
    Falls back gracefully (returns original text) if translation fails.
    """
    if not text or not text.strip():
        return text
    try:
        from deep_translator import GoogleTranslator
        # GoogleTranslator has a 5000-char limit per request; chunk if needed
        MAX_CHARS = 4500
        if len(text) <= MAX_CHARS:
            return GoogleTranslator(source=source, target=target).translate(text)
        
        # Split long text by paragraphs/lines, translate each chunk, rejoin
        chunks = []
        current_chunk = ""
        for line in text.splitlines(keepends=True):
            if len(current_chunk) + len(line) > MAX_CHARS:
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = line
            else:
                current_chunk += line
        if current_chunk:
            chunks.append(current_chunk)
        
        translator = GoogleTranslator(source=source, target=target)
        translated_chunks = [translator.translate(chunk) for chunk in chunks if chunk.strip()]
        return "".join(translated_chunks)
    except Exception as e:
        print(f"[translate_text] Translation failed: {e}")
        return text  # Return original on failure to avoid data loss

