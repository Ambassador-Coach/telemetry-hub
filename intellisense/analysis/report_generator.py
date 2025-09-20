# intellisense/analysis/report_generator.py
import logging
import json
import os
from datetime import datetime
from typing import List, Dict, Any, Optional, TYPE_CHECKING
from dataclasses import asdict  # If working with dataclass inputs directly

if TYPE_CHECKING:
    from intellisense.core.types import TestSession  # For type hinting input
    # Import StepExecutionResultSchema if ReportGenerator consumes API-like dicts
    # from intellisense.api.schemas import StepExecutionResultSchema, ValidationOutcomeSchema
    # OR import the internal StepExecutionResult and ValidationOutcome if consuming those objects
    from intellisense.capture.scenario_types import StepExecutionResult, ValidationOutcome

logger = logging.getLogger(__name__)


class IntelliSenseReportGenerator:
    def __init__(self, test_session: 'TestSession'):
        """
        Initializes the report generator with a completed TestSession object.

        Args:
            test_session: The TestSession object containing configuration and results.
        """
        if not test_session:
            raise ValueError("TestSession object cannot be None for ReportGenerator.")
        self.session = test_session
        self.report_data: Dict[str, Any] = {}  # To store structured report data

    def _extract_basic_info(self):
        """Extracts basic session information."""
        self.report_data['session_info'] = {
            "session_id": self.session.session_id,
            "session_name": self.session.config.session_name if self.session.config else "Unknown",
            "description": self.session.config.description if self.session.config else "",
            "test_date": self.session.config.test_date if self.session.config else "Unknown",
            "status": self.session.status.value if hasattr(self.session.status, 'value') else str(self.session.status),
            "created_at": self.session.created_at,
            "started_at": self.session.started_at,
            "completed_at": self.session.completed_at,
            "duration_seconds": self.session.get_duration_seconds() if hasattr(self.session, 'get_duration_seconds') else None,
            "error_messages": getattr(self.session, 'error_messages', [])
        }

    def _analyze_step_execution_results(self):
        """Analyzes MOC plan_execution_results to populate report data."""
        # IMPORTANT: TestSession.plan_execution_results stores List[Dict[str, Any]]
        # These dicts are likely from asdict(StepExecutionResult_object) or match StepExecutionResultSchema
        
        # For type safety if these are indeed dicts matching StepExecutionResultSchema:
        # from intellisense.api.schemas import StepExecutionResultSchema
        # step_results_as_objects = [StepExecutionResultSchema(**res_dict) for res_dict in self.session.plan_execution_results or []]

        # For direct use of dicts:
        step_results_dicts = getattr(self.session, 'plan_execution_results', []) or []
        
        self.report_data['moc_summary'] = {
            "total_steps_in_plan": len(step_results_dicts),
            "steps_executed": 0,  # Will count non-error steps
            "steps_with_execution_errors": 0,
            "steps_with_validation_failures": 0,
            "steps_fully_passed": 0,
        }
        
        processed_steps_report = []

        for step_dict in step_results_dicts:
            self.report_data['moc_summary']['steps_executed'] += 1
            step_report = {
                "step_id": step_dict.get("step_id"),
                "step_description": step_dict.get("step_description"),
                "master_correlation_id": step_dict.get("correlation_id"),  # Crucial for image lookup
                "execution_error": step_dict.get("step_execution_error"),
                "pipeline_timed_out": step_dict.get("pipeline_data", {}).get("timed_out", False),
                "pipeline_completed_stages": step_dict.get("pipeline_data", {}).get("completed_successfully", False),
                "total_observed_pipeline_ns": step_dict.get("pipeline_data", {}).get("latencies_ns", {}).get("total_observed_pipeline_ns"),
                "validations": [],
                "all_validations_passed_for_step": step_dict.get("all_validations_passed", True)  # Default to True if no validations
            }

            if step_report["execution_error"]:
                self.report_data['moc_summary']['steps_with_execution_errors'] += 1
                step_report["all_validations_passed_for_step"] = False  # Step error implies validation failure for step

            validation_outcomes_dicts = step_dict.get("validation_outcomes", [])
            step_had_validation_failure = False
            for vo_dict in validation_outcomes_dicts:
                step_report["validations"].append({
                    "validation_id": vo_dict.get("validation_id"),
                    "description": vo_dict.get("description"),
                    "passed": vo_dict.get("passed"),
                    "type": vo_dict.get("validation_type"),
                    "target_identifier": vo_dict.get("target_identifier"),
                    "field_checked": vo_dict.get("field_checked"),
                    "operator": vo_dict.get("operator_used"),
                    "expected_value": str(vo_dict.get("expected_value_detail"))[:200],  # Truncate
                    "actual_value": str(vo_dict.get("actual_value_detail"))[:200],   # Truncate
                    "details": vo_dict.get("details")
                })
                if not vo_dict.get("passed"):
                    step_had_validation_failure = True
            
            if step_had_validation_failure:
                self.report_data['moc_summary']['steps_with_validation_failures'] += 1
                step_report["all_validations_passed_for_step"] = False  # Override if any validation failed

            if not step_report["execution_error"] and step_report["all_validations_passed_for_step"]:
                self.report_data['moc_summary']['steps_fully_passed'] += 1
                
            processed_steps_report.append(step_report)
        
        self.report_data['processed_steps'] = processed_steps_report

    def _extract_key_latencies(self):
        """Extracts and summarizes key pipeline latencies."""
        # This would iterate through self.report_data['processed_steps'],
        # access pipeline_data.latencies_ns for each, and aggregate common latencies.
        # For V1, a simple list of total_observed_pipeline_ns might suffice.
        # A more advanced version could average specific stage-to-stage latencies.
        self.report_data['key_latencies_summary'] = {}
        all_total_latencies_ns = []
        for step_report in self.report_data.get('processed_steps', []):
            total_ns = step_report.get("total_observed_pipeline_ns")
            if total_ns is not None:
                all_total_latencies_ns.append(total_ns)
        
        if all_total_latencies_ns:
            self.report_data['key_latencies_summary']['avg_total_observed_pipeline_ms'] = \
                (sum(all_total_latencies_ns) / len(all_total_latencies_ns)) / 1_000_000.0
            # Could add P50, P95, min, max here using statistics module

    def generate_report_data(self) -> Dict[str, Any]:
        """Generates the structured report data."""
        self._extract_basic_info()
        self._analyze_step_execution_results()
        self._extract_key_latencies()
        # Add more analysis sections as needed
        return self.report_data

    def get_text_report(self) -> str:
        """Generates a simple text-based summary report."""
        if not self.report_data:
            self.generate_report_data()

        lines = []
        si = self.report_data.get('session_info', {})
        lines.append("--- IntelliSense Test Session Report ---")
        lines.append(f"Session ID: {si.get('session_id')}")
        lines.append(f"Name: {si.get('session_name')}")
        lines.append(f"Status: {si.get('status')}")

        # Handle timestamps safely
        started_at = si.get('started_at')
        completed_at = si.get('completed_at')

        if started_at:
            try:
                if isinstance(started_at, (int, float)):
                    lines.append(f"Started: {datetime.fromtimestamp(started_at).isoformat()}")
                else:
                    lines.append(f"Started: {started_at}")
            except (ValueError, OSError):
                lines.append(f"Started: {started_at}")
        else:
            lines.append("Started: N/A")

        if completed_at:
            try:
                if isinstance(completed_at, (int, float)):
                    lines.append(f"Completed: {datetime.fromtimestamp(completed_at).isoformat()}")
                else:
                    lines.append(f"Completed: {completed_at}")
            except (ValueError, OSError):
                lines.append(f"Completed: {completed_at}")
        else:
            lines.append("Completed: N/A")

        duration = si.get('duration_seconds')
        if duration is not None:
            lines.append(f"Duration: {duration:.2f}s")
        else:
            lines.append("Duration: N/A")

        # Error messages
        error_messages = si.get('error_messages', [])
        if error_messages:
            lines.append(f"Errors: {len(error_messages)} error(s) occurred")

        moc_summary = self.report_data.get('moc_summary', {})
        lines.append("\n--- MOC Execution Summary ---")
        lines.append(f"Total Steps in Plan: {moc_summary.get('total_steps_in_plan')}")
        lines.append(f"Steps Executed: {moc_summary.get('steps_executed')}")
        lines.append(f"Steps Fully Passed: {moc_summary.get('steps_fully_passed')}")
        lines.append(f"Steps with Execution Errors: {moc_summary.get('steps_with_execution_errors')}")
        lines.append(f"Steps with Validation Failures: {moc_summary.get('steps_with_validation_failures')}")

        key_lats = self.report_data.get('key_latencies_summary', {})
        if key_lats.get('avg_total_observed_pipeline_ms') is not None:
            lines.append(f"Avg. Total Observed Pipeline Latency: {key_lats['avg_total_observed_pipeline_ms']:.3f} ms")

        lines.append("\n--- Detailed Step Results ---")
        for step_rep in self.report_data.get('processed_steps', []):
            lines.append(f"\n  Step ID: {step_rep.get('step_id')} (MasterCorrID: {step_rep.get('master_correlation_id')})")
            lines.append(f"    Description: {step_rep.get('step_description')}")

            step_passed = step_rep.get('all_validations_passed_for_step') and not step_rep.get('execution_error')
            lines.append(f"    Overall Step Pass: {'PASS' if step_passed else 'FAIL'}")

            if step_rep.get('execution_error'):
                lines.append(f"    EXECUTION ERROR: {step_rep.get('execution_error')}")
            if step_rep.get('pipeline_timed_out'):
                lines.append("    PIPELINE TIMED OUT")

            total_ns = step_rep.get('total_observed_pipeline_ns')
            if total_ns is not None:
                lines.append(f"    Total Pipeline Latency: {total_ns / 1_000_000.0:.3f} ms")

            for vo in step_rep.get('validations', []):
                status = 'PASSED' if vo.get('passed') else 'FAILED'
                lines.append(f"      - Validation '{vo.get('validation_id')}': {status}")
                if not vo.get('passed'):
                    lines.append(f"        Desc: {vo.get('description')}")
                    lines.append(f"        Type: {vo.get('type')}, Target: '{vo.get('target_identifier')}', Field: '{vo.get('field_checked')}'")

                    actual_val = str(vo.get('actual_value', ''))[:50]
                    expected_val = str(vo.get('expected_value', ''))[:50]
                    operator = vo.get('operator', '==')
                    lines.append(f"        Check: Actual '{actual_val}' {operator} Expected '{expected_val}'")

                    details = vo.get('details')
                    if details:
                        lines.append(f"        Details: {details}")

        return "\n".join(lines)

    def get_json_report(self) -> str:
        """Generates a JSON report."""
        if not self.report_data:
            self.generate_report_data()
        return json.dumps(self.report_data, indent=2, default=str)  # default=str for non-serializable like enums if any remain

    def save_reports(self, base_filename_prefix: Optional[str] = None) -> Dict[str, str]:
        """Saves reports to files in the session's output directory."""
        if not self.session.config or not self.session.config.output_directory:
            logger.warning("Output directory not set in session config. Cannot save reports.")
            return {}

        os.makedirs(self.session.config.output_directory, exist_ok=True)

        prefix = base_filename_prefix or self.session.session_id
        paths: Dict[str, str] = {}

        try:
            # JSON Data
            json_data_path = os.path.join(self.session.config.output_directory, f"{prefix}_report_data.json")
            with open(json_data_path, 'w', encoding='utf-8') as f:
                json.dump(self.report_data if self.report_data else self.generate_report_data(), f, indent=2, default=str)
            paths['json_data'] = json_data_path

            # Text Report
            txt_report_path = os.path.join(self.session.config.output_directory, f"{prefix}_summary.txt")
            with open(txt_report_path, 'w', encoding='utf-8') as f:
                f.write(self.get_text_report())
            paths['text_summary'] = txt_report_path

            logger.info(f"Reports saved to: {self.session.config.output_directory}")

        except Exception as e:
            logger.error(f"Error saving reports: {e}", exc_info=True)
            raise

        return paths


# Example Usage (would be invoked by MasterController or API endpoint)
# if __name__ == '__main__':
#     # Mock TestSession and StepExecutionResultSchema for testing
#     # from intellisense.core.types import TestSession, TestSessionConfig, TestStatus
#     # from intellisense.api.schemas import StepExecutionResultSchema, ValidationOutcomeSchema
#     # mock_config = TestSessionConfig(...)
#     # mock_session = TestSession(session_id="mock_session_001", config=mock_config, status=TestStatus.COMPLETED)
#     # mock_session.plan_execution_results = [
#     #     asdict(StepExecutionResultSchema(step_id="step1", correlation_id="corr1", pipeline_data={"latencies_ns": {"total_observed_pipeline_ns": 123456789}}, validation_outcomes=[asdict(ValidationOutcomeSchema(validation_id="v1", passed=True, validation_type="type", operator_used="=="))]))
#     # ]
#     # mock_session.config.output_directory = "./temp_reports"
#     # generator = IntelliSenseReportGenerator(mock_session)
#     # print(generator.get_text_report())
#     # generator.save_reports()
