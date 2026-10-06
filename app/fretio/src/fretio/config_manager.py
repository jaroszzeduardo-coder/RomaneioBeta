"""
Centralized configuration management for Fretio.

Handles loading, caching, and hot-reload of TOML configuration files
with support for multiple companies and fallback defaults.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from fretio.logging_conf import bind_logger, get_logger

try:
    import tomllib  # py311+
except ImportError:
    tomllib = None

logger = get_logger(__name__)


# Default configuration fallback (TOML format as string)
CONFIG_FALLBACK = """[fretio]
fator_cubagem = 6000
cache_dir = "cache"

[romaneio]
cep_origem = "99740000"
cnpj_pagador_padrao = ""

[transportadoras.braspress]
habilitado = true
cnpj = ""
senha = ""
ufs_atendidas = ["AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO"]

[transportadoras.trd]
habilitado = true
email = ""
senha = ""
headless = true
volumes = 1
altura = 0.1
largura = 0.1
comprimento = 0.1
ufs_atendidas = ["RS", "SC", "PR", "SP", "MG", "ES", "RJ"]

[transportadoras.agex]
habilitado = false
email = ""
senha = ""
cnpj_remetente = ""
cnpj_destinatario = ""
ufs_atendidas = ["PR", "SP", "GO", "DF", "TO", "PA", "MT", "MS"]

[transportadoras.eucatur]
habilitado = false
dominio = ""
usuario = ""
senha = ""
ufs_atendidas = ["RR", "AM", "AC", "RO", "MT", "MS"]

[transportadoras.rodonaves]
habilitado = false
dominio = "RTE"
usuario = ""
senha = ""
cnpj_pagador = ""
login_url = "https://rodonaves.com.br/portaldocliente"
cotacao_url = "https://rodonaves.com.br/cotacao"
headless = true
ufs_atendidas = ["AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO"]

[transportadoras.alfa]
habilitado = false
login = ""
senha = ""
cnpj_remetente = ""
login_url = "https://areadocliente.alfatransportes.com.br/login/"
cotacao_url = "https://areadocliente.alfatransportes.com.br/cotacao/api/"
headless = false
ufs_atendidas = ["AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO"]

[transportadoras.coopex]
habilitado = false
dominio = ""
usuario = ""
senha = ""
ufs_atendidas = []

[transportadoras.translovato]
habilitado = false
cnpj = ""
usuario = ""
senha = ""
cnpj_remetente = ""
produto = "CONFECCAO"
headless = true
cotacao_url = "https://www.translovato.com.br/fale-conosco/solicitacao-de-cotacao"
ufs_atendidas = ["AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO"]
"""


class ConfigManager:
    """
    Centralized configuration manager for Fretio.
    
    Supports:
    - Loading from multiple config file locations with precedence
    - Caching to avoid repeated file I/O
    - Hot-reload detection when files change
    - Fallback to hardcoded defaults
    - Per-company configuration isolation
    
    Configuration precedence (first found wins):
    1. %APPDATA%\\Fretio\\empresas\\{empresa}\\CONFIG.toml
    2. Relative paths (if configured)
    3. Fallback defaults (hardcoded)
    """
    
    # Singleton instances per empresa
    _instances: dict[str, ConfigManager] = {}
    _instances_lock = threading.Lock()
    
    # Configuration cache
    _config_cache: dict[str, tuple[dict[str, Any], float]] = {}
    _cache_lock = threading.Lock()
    
    # File modification times for hot-reload detection
    _file_mtimes: dict[str, float] = {}
    _cache_clearers: set[Callable[[], None]] = set()
    _cache_clearers_lock = threading.Lock()
    
    def __init__(self, empresa_nome: str) -> None:
        """
        Initialize ConfigManager for a specific company.
        
        Args:
            empresa_nome: Company name (used in config paths)
        """
        self.empresa_nome = empresa_nome
        self._config_path: Optional[Path] = None
        self._config_data: dict[str, Any] = {}
        self._load_lock = threading.RLock()
        self._cache_generation = 0

    def _logger(self, *, operation: str):
        return bind_logger(logger, empresa=self.empresa_nome, operation=operation)
    
    @classmethod
    def get_instance(cls, empresa_nome: str) -> ConfigManager:
        """
        Get or create a singleton ConfigManager instance for a company.
        
        Args:
            empresa_nome: Company name
            
        Returns:
            ConfigManager instance for the company
        """
        if empresa_nome not in cls._instances:
            with cls._instances_lock:
                if empresa_nome not in cls._instances:
                    cls._instances[empresa_nome] = cls(empresa_nome)
                    bind_logger(logger, empresa=empresa_nome, operation="config_manager_init").debug(
                        "ConfigManager instance criada"
                    )
        return cls._instances[empresa_nome]
    
    def _base_dir(self) -> Path:
        """Get the base directory (app installation or sys.executable)."""
        return (
            Path(sys.executable).resolve().parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent.parent.parent.parent  # Go up to app/
        )
    
    def _get_config_paths(self) -> list[Path]:
        """
        Get configuration file paths in precedence order.
        
        Returns:
            List of Path objects to check for configuration
        """
        paths: list[Path] = []
        
        # 1. APPDATA/Fretio/empresas/{empresa}/CONFIG.toml (highest priority)
        appdata = os.getenv("APPDATA")
        if appdata:
            empresa_config = Path(appdata) / "Fretio" / "empresas" / self.empresa_nome / "CONFIG.toml"
            paths.append(empresa_config)
        
        # 2. Base directory paths
        base = self._base_dir()
        paths.extend([
            base / "Fretio" / "CONFIG.toml",
            base / "CONFIG.toml",
            Path.cwd() / "Fretio" / "CONFIG.toml",
            Path.cwd() / "CONFIG.toml",
        ])
        
        # 3. PROGRAMDATA as fallback
        programdata = os.getenv("PROGRAMDATA")
        if programdata:
            paths.append(Path(programdata) / "Fretio" / "CONFIG.toml")
        
        return paths
    
    def _load_toml_file(self, path: Path) -> Optional[dict[str, Any]]:
        """
        Load a TOML file and return parsed dictionary.
        
        Args:
            path: Path to TOML file
            
        Returns:
            Parsed configuration dict or None if load fails
        """
        if tomllib is None:
            self._logger(operation="load_config_file").warning("tomllib unavailable; cannot load %s", path)
            return None
        
        if not path.exists():
            return None
        
        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
                if isinstance(data, dict):
                    self._logger(operation="load_config_file").info("CONFIG loaded from: %s", path)
                    self._config_path = path
                    # Store file mtime for hot-reload detection
                    self._file_mtimes[str(path)] = path.stat().st_mtime
                    return data
        except Exception as e:
            self._logger(operation="load_config_file").error("Failed to load CONFIG from %s: %s", path, e)
        
        return None
    
    def _parse_fallback(self) -> dict[str, Any]:
        """
        Parse the fallback configuration string.
        
        Returns:
            Parsed fallback configuration dict
        """
        if tomllib is None:
            self._logger(operation="parse_fallback").warning("tomllib unavailable; cannot parse fallback config")
            return {}
        
        try:
            # tomllib.loads expects a string (not bytes)
            data = tomllib.loads(CONFIG_FALLBACK)
            if isinstance(data, dict):
                self._logger(operation="parse_fallback").info("Using fallback configuration")
                return data
        except Exception as e:
            self._logger(operation="parse_fallback").error("Failed to parse fallback config: %s", e)
        
        return {}

    def _apply_secure_credentials(self, config: dict[str, Any]) -> dict[str, Any]:
        """Overlay credentials from OS secure storage without changing public config shape."""
        try:
            import secure_credentials  # type: ignore[import-not-found]

            secure_credentials.migrate_plaintext_credentials(config, self.empresa_nome)
            return secure_credentials.overlay_secure_credentials(config, self.empresa_nome)
        except Exception as exc:
            self._logger(operation="secure_credentials").warning(
                "Credenciais seguras indisponíveis; usando CONFIG.toml: %s",
                exc,
            )
            return config
    
    def _check_hot_reload(self) -> bool:
        """
        Check if configuration file has been modified since last load.
        
        Returns:
            True if file was modified, False otherwise
        """
        if not self._config_path or not self._config_path.exists():
            return False
        
        path_str = str(self._config_path)
        try:
            current_mtime = self._config_path.stat().st_mtime
            last_mtime = self._file_mtimes.get(path_str, 0)
            
            if current_mtime > last_mtime:
                self._logger(operation="hot_reload").info("CONFIG file changed, hot-reloading: %s", self._config_path)
                self._file_mtimes[path_str] = current_mtime
                return True
        except Exception as e:
            self._logger(operation="hot_reload").error("Failed to check file modification time: %s", e)
        
        return False

    @classmethod
    def register_cache_clearer(cls, clearer: Callable[[], None]) -> Callable[[], None]:
        """Register a callback to clear derived caches when config is invalidated."""
        with cls._cache_clearers_lock:
            cls._cache_clearers.add(clearer)
        return clearer

    @classmethod
    def unregister_cache_clearer(cls, clearer: Callable[[], None]) -> None:
        """Remove a previously registered cache clearer."""
        with cls._cache_clearers_lock:
            cls._cache_clearers.discard(clearer)

    @classmethod
    def _clear_registered_caches(cls) -> None:
        """Clear all registered derived caches."""
        with cls._cache_clearers_lock:
            clearers = tuple(cls._cache_clearers)
        for clearer in clearers:
            try:
                clearer()
            except Exception as exc:
                self_logger = bind_logger(logger, operation="clear_registered_caches")
                self_logger.warning("Failed to clear registered cache: %s", exc)

    def _invalidate_cached_config(self, *, clear_loaded_path: bool) -> None:
        """Invalidate cached config data and derived caches."""
        self._config_data = {}
        if clear_loaded_path:
            self._config_path = None
        self._cache_generation += 1
        self._clear_registered_caches()
    
    def load_config(self) -> dict[str, Any]:
        """
        Load and cache configuration.
        
        Supports hot-reload: if the underlying file has changed, it will be
        reloaded automatically.
        
        Configuration precedence:
        1. Found config file in standard locations
        2. Fallback defaults (hardcoded)
        
        Returns:
            Configuration dictionary
        """
        with self._load_lock:
            # Check for hot-reload
            if self._config_path and self._check_hot_reload():
                self._invalidate_cached_config(clear_loaded_path=False)
            
            # Return cached config if available and file hasn't changed
            if self._config_data:
                self._logger(operation="load_config").debug("Returning cached configuration")
                return self._config_data
            
            # Try each config path in order
            for config_path in self._get_config_paths():
                config = self._load_toml_file(config_path)
                if config is not None:
                    self._config_data = self._apply_secure_credentials(config)
                    return self._config_data
            
            # Fallback to hardcoded defaults
            self._config_data = self._apply_secure_credentials(self._parse_fallback())
            return self._config_data
    
    def get(
        self,
        section: str,
        key: str,
        default: Any = None
    ) -> Any:
        """
        Get a configuration value with section and key.
        
        Args:
            section: Top-level configuration section (e.g., "fretio", "romaneio")
            key: Key within the section
            default: Default value if not found
            
        Returns:
            Configuration value or default
        """
        config = self.load_config()
        if not isinstance(config, dict):
            return default
        
        section_data = config.get(section, {})
        if not isinstance(section_data, dict):
            return default
        
        return section_data.get(key, default)
    
    def get_section(self, section: str) -> dict[str, Any]:
        """
        Get an entire configuration section.
        
        Args:
            section: Configuration section name
            
        Returns:
            Section dictionary or empty dict if not found
        """
        config = self.load_config()
        if not isinstance(config, dict):
            return {}
        
        section_data = config.get(section, {})
        if isinstance(section_data, dict):
            return section_data
        
        return {}
    
    def validate(self) -> bool:
        """
        Validate that configuration is valid and usable.
        
        Returns:
            True if configuration is valid, False otherwise
        """
        try:
            config = self.load_config()
            
            if not isinstance(config, dict):
                self._logger(operation="validate_config").error("Configuration is not a dictionary")
                return False
            
            # Basic validation: check for required sections
            if "fretio" not in config:
                self._logger(operation="validate_config").warning("Missing 'fretio' section in configuration")
            
            self._logger(operation="validate_config").info("Configuration validation passed")
            return True
        
        except Exception as e:
            self._logger(operation="validate_config").error("Configuration validation failed: %s", e)
            return False
    
    def get_fallback(self) -> dict[str, Any]:
        """
        Get the hardcoded fallback configuration.
        
        Returns:
            Fallback configuration dictionary
        """
        return self._parse_fallback()
    
    def get_loaded_path(self) -> Optional[Path]:
        """
        Get the path to the configuration file that was loaded.
        
        Returns:
            Path to loaded config file or None if using fallback
        """
        return self._config_path

    def get_cache_token(self) -> tuple[str, int]:
        """Return a token that changes whenever cached config is invalidated."""
        return (self.empresa_nome, self._cache_generation)
    
    def reload(self) -> dict[str, Any]:
        """
        Force reload of configuration from disk.
        
        Returns:
            Reloaded configuration dictionary
        """
        with self._load_lock:
            self._invalidate_cached_config(clear_loaded_path=True)
            self._logger(operation="reload_config").info("Forcing configuration reload")
            return self.load_config()
    
    def __repr__(self) -> str:
        """String representation of ConfigManager."""
        path_str = str(self._config_path) if self._config_path else "fallback"
        return f"<ConfigManager empresa={self.empresa_nome} config={path_str}>"
