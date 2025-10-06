
from fastapi.templating import Jinja2Templates

from app.settings import get_settings

templates = Jinja2Templates(directory=str(get_settings().templates_dir))
