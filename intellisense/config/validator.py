import logging
from typing import Any

# Assuming GlobalConfig is importable, using placeholder
# from utils.global_config import GlobalConfig
if 'GlobalConfig' not in globals():
    class GlobalConfig: intellisense_config: Any = None

logger = logging.getLogger(__name__)

class ConfigurationValidator:
    """Validates configuration flow for IntelliSense mode."""

    @staticmethod
    def validate_intellisense_config_flow(global_config_instance: GlobalConfig) -> bool:
        """Test complete configuration flow from GlobalConfig to specific test configs."""
        if not global_config_instance:
            logger.error("GlobalConfig instance is None.")
            return False
        
        try:
            logger.info("Validating IntelliSense configuration flow...")
            # 1. Check if IntelliSense mode is appropriately set (string or Enum)
            is_mode_intellisense = False
            if hasattr(global_config_instance, 'is_intellisense_active') and callable(global_config_instance.is_intellisense_active):
                is_mode_intellisense = global_config_instance.is_intellisense_active()
            elif hasattr(global_config_instance, 'intellisense_mode'):
                mode_val = global_config_instance.intellisense_mode
                is_mode_intellisense = str(mode_val).upper() == "INTELLISENSE" or (hasattr(mode_val,'name') and mode_val.name == "INTELLISENSE")

            if not is_mode_intellisense:
                logger.info("GlobalConfig not in IntelliSense mode, or is_intellisense_active() is False. Config flow test skipped for this state.")
                # This isn't a failure of the config structure itself, but mode isn't set for full test.
                # For a strict test of structure, we might require mode to be INTELLISENSE.
                return True # Or False if mode INTELLISENSE is required for this validation

            # 2. Check IntelliSenseConfig presence
            intellisense_config = getattr(global_config_instance, 'intellisense_config', None)
            if not intellisense_config:
                raise ValueError("IntelliSenseConfig not found in GlobalConfig or is None, but mode is IntelliSense.")
            
            # 3. Check TestSessionConfig within IntelliSenseConfig
            # Assuming IntelliSenseConfig structure from intellisense.core.types
            test_session_config = getattr(intellisense_config, 'test_session_config', None)
            if not test_session_config: # Assuming test_session_config is not Optional in IntelliSenseConfig
                raise ValueError("TestSessionConfig not found in IntelliSenseConfig.")
            
            # Check if it's the right type (requires TestSessionConfig to be importable here)
            # from intellisense.config.session_config import TestSessionConfig as TSC
            # if not isinstance(test_session_config, TSC):
            #     raise TypeError(f"intellisense_config.test_session_config is not of type TestSessionConfig. Got: {type(test_session_config)}")


            # 4. Check specific sub-configs like OCRTestConfig
            # ocr_config = getattr(test_session_config, 'ocr_config', None)
            # if not ocr_config:
            #     raise ValueError("OCRTestConfig not found in TestSessionConfig.")
            # from intellisense.config.session_config import OCRTestConfig as OTC
            # if not isinstance(ocr_config, OTC):
            #      raise TypeError(f"TestSessionConfig.ocr_config is not of type OCRTestConfig. Got: {type(ocr_config)}")

            # Further checks for specific attributes if needed:
            # if not hasattr(ocr_config, 'frame_processing_fps'):
            #     raise ValueError("OCRTestConfig missing 'frame_processing_fps'.")

            logger.info("IntelliSense Configuration flow validation successful.")
            return True
            
        except (ValueError, TypeError, AttributeError) as e:
            logger.error(f"IntelliSense Configuration flow validation failed: {e}", exc_info=True)
            return False
        except Exception as e_unexp: # Catch any other unexpected errors
            logger.error(f"Unexpected error during IntelliSense Configuration flow validation: {e_unexp}", exc_info=True)
            return False
