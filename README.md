# Pyserver

PythonAnywhere 업로드용 턴키 폴더입니다.

포함 내용:

- `app/`: Flask API
- `static/`: Flutter web 빌드 결과
- `database/`: 초기 seed CSV
- `data/`: SQLite DB 생성 위치
- `generated/`: PDF 생성 위치
- `fonts/`: 한글 PDF 폰트
- `app.py`: 로컬 실행 엔트리
- `wsgi.py`: PythonAnywhere WSGI import 엔트리

## 로컬 실행

```powershell
cd C:\Users\82102\Desktop\dev_main\Obsidian_Food_OEM_ODM\Pyserver
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
```

브라우저:

- `http://127.0.0.1:8010/`
- `http://127.0.0.1:8010/api/health`

## PythonAnywhere 배포

1. `Pyserver` 폴더 전체를 업로드합니다.
2. 가상환경을 만들고 `pip install -r requirements.txt`를 실행합니다.
3. `.env.example`을 `.env`로 복사하고 필요 시 `SAM_API_KEY`를 채웁니다.
4. Web app의 WSGI 파일을 아래처럼 맞춥니다.

```python
import sys

project_home = "/home/<username>/Pyserver"
if project_home not in sys.path:
    sys.path.insert(0, project_home)

from wsgi import application
```

5. Reload 후 `/`와 `/api/health`를 확인합니다.

## 메모

- 현재 Flutter build는 same-origin API 기준입니다. 프론트와 백엔드를 같은 도메인 루트에 올려야 합니다.
- 현재 build의 `<base href>`는 `/`입니다. 서브경로 배포가 필요하면 Flutter web을 `--base-href /subpath/`로 다시 빌드해야 합니다.
- `data/`와 `generated/`는 쓰기 가능해야 합니다.
