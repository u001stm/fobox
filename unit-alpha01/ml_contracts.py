"""Validation for the JSON Schema subset used by our own versioned contracts."""
from datetime import datetime
import json
import math
from pathlib import Path
import re


def validate(kind, value):
    root = json.loads(Path(__file__).with_name('ml_schema.json').read_text('utf-8'))
    def check(schema, data, location):
        if '$ref' in schema:
            node = root
            for key in schema['$ref'].removeprefix('#/').split('/'):
                node = node[key]
            return check(node, data, location)
        types = schema.get('type', [])
        types = [types] if isinstance(types, str) else types
        allowed = {'object': isinstance(data, dict), 'array': isinstance(data, list), 'string': isinstance(data, str),
                   'number': isinstance(data, (float, int)) and not isinstance(data, bool) and math.isfinite(data),
                   'integer': isinstance(data, int) and not isinstance(data, bool), 'boolean': isinstance(data, bool), 'null': data is None}
        if types and not any(allowed[t] for t in types):
            raise ValueError(location + ': неверный тип')
        if 'enum' in schema and data not in schema['enum']:
            raise ValueError(location + ': неизвестное значение')
        if 'const' in schema and data != schema['const']:
            raise ValueError(location + ': неверная версия контракта')
        if isinstance(data, str):
            if len(data) > schema.get('maxLength', 100000):raise ValueError(location + ': слишком длинно')
            if 'pattern' in schema and not re.search(schema['pattern'], data):raise ValueError(location + ': неверный формат')
            if schema.get('format') == 'date-time':
                date = datetime.fromisoformat(data.replace('Z', '+00:00'))
                if date.tzinfo is None:raise ValueError(location + ': нужна зона')
        if isinstance(data, (int, float)) and not isinstance(data, bool):
            if 'minimum' in schema and data < schema['minimum'] or 'maximum' in schema and data > schema['maximum']:
                raise ValueError(location + ': вне диапазона')
        if isinstance(data, dict):
            for key in schema.get('required', []):
                if key not in data:raise ValueError(location + ': нет ' + key)
            props = schema.get('properties', {})
            for key, item in data.items():
                if key in props:check(props[key], item, location + '.' + key)
                elif schema.get('additionalProperties') is False:raise ValueError(location + ': неизвестное поле ' + key)
                elif isinstance(schema.get('additionalProperties'), dict):check(schema['additionalProperties'], item, location + '.' + key)
        if isinstance(data, list):
            if len(data) > schema.get('maxItems', 100000):raise ValueError(location + ': слишком много элементов')
            if len(data) < schema.get('minItems', 0):raise ValueError(location + ': нет элементов')
            for index, item in enumerate(data):check(schema.get('items', {}), item, location + f'[{index}]')
    check(root['$defs'][kind], value, kind)
    json.dumps(value, allow_nan=False)


def validate_result(result):
    validate('assets', {'assets': result['assets']})
    validate('timeline', result['timeline'])
    validate('stats', result['stats'])
    ids = {a['asset_id'] for a in result['assets']}
    if len(ids) != len(result['assets']):raise ValueError('Повторяющиеся asset ID')
    items = result['timeline']['items']
    if len({r['item_id'] for r in items}) != len(items):raise ValueError('Повторяющиеся item ID')
    if any(row['asset_id'] not in ids for row in items):raise ValueError('Timeline ссылается на неизвестный asset')
    bins = result['stats']['activity']['bins']
    if sum(b['count'] for b in bins) != result['stats']['activity_capture_count']:
        raise ValueError('Количество capture не совпало с activity bins')
