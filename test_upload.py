import requests
import json

files = {'file': ('test.xlsx', b'dummy content', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')}
data = {'form_api_key': 'test_key'}
try:
    resp = requests.post('http://localhost:8000/process', files=files, data=data)
    print("Status:", resp.status_code)
    print("Text:", resp.text)
except Exception as e:
    print("Exception:", e)
