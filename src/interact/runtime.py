from interact.config import Config
from interact.data import PackageData
from interact.formats import CoordFormat
from interact.models import CircuitBreaker, Model

config = Config()
breaker = CircuitBreaker()

CoordFormat.load_from_config(PackageData.models_data().get("coordFormats", {}))
Model.load_registry()
