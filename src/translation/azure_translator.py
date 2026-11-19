"""Azure text translation for unresolved Chinese-to-Vietnamese batches."""
import requests

from config.settings import settings
from src.jobs.logger import TaskCancelled


class AzureTranslator:
    name = 'azure_translator'
    version = 'azure-text-v3-zh-hans-vi'

    def translate_cues(self, cues, output_dir=None, should_continue=None):
        headers = {'Ocp-Apim-Subscription-Key': settings.AZURE_TRANSLATOR_KEY}
        if settings.AZURE_TRANSLATOR_REGION:
            headers['Ocp-Apim-Subscription-Region'] = settings.AZURE_TRANSLATOR_REGION
        for offset in range(0, len(cues), 50):
            if should_continue and not should_continue():
                raise TaskCancelled()
            batch = cues[offset:offset + 50]
            response = requests.post(
                'https://api.cognitive.microsofttranslator.com/translate',
                params={'api-version': '3.0', 'from': 'zh-Hans', 'to': 'vi'},
                headers=headers, json=[{'Text': cue.effective_source_text} for cue in batch],
                timeout=(5, 20), allow_redirects=False,
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or len(rows) != len(batch):
                raise ValueError('Azure trả về sai số câu')
            for cue, row in zip(batch, rows):
                translations = row['translations']
                if len(translations) != 1 or translations[0]['to'] != 'vi':
                    raise ValueError('Azure trả về sai ngôn ngữ đích')
                cue.vi_text = translations[0]['text']
        return cues
