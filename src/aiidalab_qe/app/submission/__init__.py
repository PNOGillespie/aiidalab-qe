# -*- coding: utf-8 -*-
"""Widgets for the submission of bands work chains.

Authors: AiiDAlab team
"""
from __future__ import annotations

import os

import ipywidgets as ipw
import traitlets as tl
from aiida import orm
from aiida.common import NotExistent
from aiida.engine import ProcessBuilderNamespace, submit
from aiidalab_widgets_base import ComputationalResourcesWidget, WizardAppWidgetStep
from IPython.display import display

from aiidalab_qe.app.parameters import DEFAULT_PARAMETERS
from aiidalab_qe.common.setup_codes import QESetupWidget
from aiidalab_qe.workflows import QeAppWorkChain

from .resource import ParallelizationSettings, ResourceSelectionWidget
from .sssp import SSSPInstallWidget

PROTOCOL_PSEUDO_MAP = {
    "fast": "SSSP/1.2/PBE/efficiency",
    "moderate": "SSSP/1.2/PBE/efficiency",
    "precise": "SSSP/1.2/PBE/precision",
}


class SubmitQeAppWorkChainStep(ipw.VBox, WizardAppWidgetStep):
    """Step for submission of a bands workchain."""

    codes_title = ipw.HTML(
        """<div style="padding-top: 0px; padding-bottom: 0px">
        <h4>Codes</h4></div>"""
    )
    codes_help = ipw.HTML(
        """<div style="line-height: 140%; padding-top: 0px; padding-bottom:
        10px"> Select the code to use for running the calculations. The codes
        on the local machine (localhost) are installed by default, but you can
        configure new ones on potentially more powerful machines by clicking on
        "Setup new code".</div>"""
    )

    # This number provides a rough estimate for how many MPI tasks are needed
    # for a given structure.
    NUM_SITES_PER_MPI_TASK_DEFAULT = 6

    # Warn the user if they are trying to run calculations for a large
    # structure on localhost.
    RUN_ON_LOCALHOST_NUM_SITES_WARN_THRESHOLD = 10

    # Put a limit on how many MPI tasks you want to run per k-pool by default
    MAX_MPI_PER_POOL = 20

    input_structure = tl.Instance(orm.StructureData, allow_none=True)
    process = tl.Instance(orm.WorkChainNode, allow_none=True)
    previous_step_state = tl.UseEnum(WizardAppWidgetStep.State)
    input_parameters = tl.Dict()
    _submission_blockers = tl.List(tl.Unicode())

    def __init__(self, qe_auto_setup=True, **kwargs):
        self.message_area = ipw.Output()
        self._submission_blocker_messages = ipw.HTML()

        self.pw_code = ComputationalResourcesWidget(
            description="pw.x:", default_calc_job_plugin="quantumespresso.pw"
        )
        self.dos_code = ComputationalResourcesWidget(
            description="dos.x:",
            default_calc_job_plugin="quantumespresso.dos",
        )
        self.projwfc_code = ComputationalResourcesWidget(
            description="projwfc.x:",
            default_calc_job_plugin="quantumespresso.projwfc",
        )

        self.resources_config = ResourceSelectionWidget()
        self.parallelization = ParallelizationSettings()

        self.set_selected_codes(DEFAULT_PARAMETERS["codes"])
        self.set_resource_defaults()

        self.pw_code.observe(self._update_state, "value")
        self.pw_code.observe(self._update_resources, "value")
        self.dos_code.observe(self._update_state, "value")
        self.projwfc_code.observe(self._update_state, "value")

        self.submit_button = ipw.Button(
            description="Submit",
            tooltip="Submit the calculation with the selected parameters.",
            icon="play",
            button_style="success",
            layout=ipw.Layout(width="auto", flex="1 1 auto"),
            disabled=True,
        )

        self.submit_button.on_click(self._on_submit_button_clicked)

        # The SSSP installation status widget shows the installation status of
        # the SSSP pseudo potentials and triggers the installation in case that
        # they are not yet installed. The widget will remain in a "busy" state
        # in case that the installation was already triggered elsewhere, e.g.,
        # by the start up scripts.  The submission is blocked while the
        # potentials are not yet installed.
        self.sssp_installation_status = SSSPInstallWidget(auto_start=qe_auto_setup)
        self.sssp_installation_status.observe(self._update_state, ["busy", "installed"])
        self.sssp_installation_status.observe(self._toggle_install_widgets, "installed")

        # The QE setup widget checks whether there are codes that match specific
        # expected labels (e.g. "pw-7.2@localhost") and triggers both the
        # installation of QE into a dedicated conda environment and the setup of
        # the codes in case that they are not already configured.
        self.qe_setup_status = QESetupWidget(auto_start=qe_auto_setup)
        self.qe_setup_status.observe(self._update_state, "busy")
        self.qe_setup_status.observe(self._toggle_install_widgets, "installed")
        self.qe_setup_status.observe(self._auto_select_code, "installed")
        self.ui_parameters = {}

        super().__init__(
            children=[
                self.codes_title,
                self.codes_help,
                self.pw_code,
                self.dos_code,
                self.projwfc_code,
                self.resources_config,
                self.parallelization,
                self.message_area,
                self.sssp_installation_status,
                self.qe_setup_status,
                self._submission_blocker_messages,
                self.submit_button,
            ]
        )

    @tl.observe("_submission_blockers")
    def _observe_submission_blockers(self, change):
        if change["new"]:
            fmt_list = "\n".join((f"<li>{item}</li>" for item in sorted(change["new"])))
            self._submission_blocker_messages.value = f"""
                <div class="alert alert-info">
                <strong>The submission is blocked, due to the following reason(s):</strong>
                <ul>{fmt_list}</ul></div>"""
        else:
            self._submission_blocker_messages.value = ""

    def _identify_submission_blockers(self):
        # Do not submit while any of the background setup processes are running.
        if self.qe_setup_status.busy or self.sssp_installation_status.busy:
            yield "Background setup processes must finish."

        # No code selected (this is ignored while the setup process is running).
        if self.pw_code.value is None and not self.qe_setup_status.busy:
            yield ("No pw code selected")

        # No code selected for pdos (this is ignored while the setup process is running).
        if (
            "pdos" in self.input_parameters.get("workchain", {}).get("properties", [])
            and (self.dos_code.value is None or self.projwfc_code.value is None)
            and not self.qe_setup_status.busy
        ):
            yield "Calculating the PDOS requires both dos.x and projwfc.x to be set."

        # SSSP library not installed
        if not self.sssp_installation_status.installed:
            yield "The SSSP library is not installed."

        if (
            "pdos" in self.input_parameters.get("workchain", {}).get("properties", [])
            and not any(
                [
                    self.pw_code.value is None,
                    self.dos_code.value is None,
                    self.projwfc_code.value is None,
                ]
            )
            and len(
                set(
                    (
                        orm.load_code(self.pw_code.value).computer.pk,
                        orm.load_code(self.dos_code.value).computer.pk,
                        orm.load_code(self.projwfc_code.value).computer.pk,
                    )
                )
            )
            != 1
        ):
            yield (
                "All selected codes must be installed on the same computer. This is because the "
                "PDOS calculations rely on large files that are not retrieved by AiiDA."
            )

    def _update_state(self, _=None):
        # If the previous step has failed, this should fail as well.
        if self.previous_step_state is self.State.FAIL:
            self.state = self.State.FAIL
            return
        # Do not interact with the user if they haven't successfully completed the previous step.
        elif self.previous_step_state is not self.State.SUCCESS:
            self.state = self.State.INIT
            return

        # Process is already running.
        if self.process is not None:
            self.state = self.State.SUCCESS
            return

        blockers = list(self._identify_submission_blockers())
        if any(blockers):
            self._submission_blockers = blockers
            self.state = self.State.READY
            return

        self._submission_blockers = []
        self.state = self.state.CONFIGURED

    def _toggle_install_widgets(self, change):
        if change["new"]:
            self.children = [
                child for child in self.children if child is not change["owner"]
            ]

    def _auto_select_code(self, change):
        if change["new"] and not change["old"]:
            for code in [
                "pw",
                "dos",
                "projwfc",
            ]:
                try:
                    code_widget = getattr(self, f"{code}_code")
                    code_widget.refresh()
                    code_widget.value = orm.load_code(
                        DEFAULT_PARAMETERS["codes"][code]
                    ).uuid
                except NotExistent:
                    pass

    _ALERT_MESSAGE = """
        <div class="alert alert-{alert_class} alert-dismissible">
        <a href="#" class="close" data-dismiss="alert" aria-label="close">&times;</a>
        <span class="closebtn" onclick="this.parentElement.style.display='none';">&times;</span>
        <strong>{message}</strong>
        </div>"""

    def _show_alert_message(self, message, alert_class="info"):
        with self.message_area:
            display(
                ipw.HTML(
                    self._ALERT_MESSAGE.format(alert_class=alert_class, message=message)
                )
            )

    def _update_resources(self, change):
        if change["new"] and (
            change["old"] is None
            or orm.load_code(change["new"]).computer.pk
            != orm.load_code(change["old"]).computer.pk
        ):
            self.set_resource_defaults(orm.load_code(change["new"]).computer)

    def get_resources(self):
        resources = {
            "num_machines": self.resources_config.num_nodes.value,
            "num_mpiprocs_per_machine": self.resources_config.num_cpus.value,
            "npools": self.parallelization.npools.value,
        }
        return resources

    def set_resources(self, resources):
        self.resources_config.num_nodes.value = resources["num_machines"]
        self.resources_config.num_cpus.value = resources["num_mpiprocs_per_machine"]
        self.parallelization.npools.value = resources["npools"]

    def set_resource_defaults(self, computer=None):
        if computer is None or computer.hostname == "localhost":
            self.resources_config.num_nodes.disabled = True
            self.resources_config.num_nodes.value = 1
            self.resources_config.num_cpus.max = os.cpu_count()
            self.resources_config.num_cpus.value = 1
            self.resources_config.num_cpus.description = "CPUs"
            self.parallelization.npools.value = 1
        else:
            default_mpiprocs = computer.get_default_mpiprocs_per_machine()
            self.resources_config.num_nodes.disabled = False
            self.resources_config.num_cpus.max = default_mpiprocs
            self.resources_config.num_cpus.value = default_mpiprocs
            self.resources_config.num_cpus.description = "CPUs/node"
            self.parallelization.npools.value = self._get_default_parallelization()

        self._check_resources()

    def _get_default_parallelization(self):
        """A _very_ rudimentary approach for obtaining a minimal npools setting."""
        num_mpiprocs = (
            self.resources_config.num_nodes.value * self.resources_config.num_cpus.value
        )

        for i in range(1, num_mpiprocs + 1):
            if num_mpiprocs % i == 0 and num_mpiprocs // i < self.MAX_MPI_PER_POOL:
                return i

    def _check_resources(self):
        """Check whether the currently selected resources will be sufficient and warn if not."""
        if not self.pw_code.value:
            return  # No code selected, nothing to do.

        num_cpus = self.resources_config.num_cpus.value
        on_localhost = (
            orm.load_node(self.pw_code.value).computer.hostname == "localhost"
        )
        if self.pw_code.value and on_localhost and num_cpus > 1:
            self._show_alert_message(
                "The selected code would be executed on the local host, but "
                "the number of CPUs is larger than one. Please review "
                "the configuration and consider to select a code that runs "
                "on a larger system if necessary.",
                alert_class="warning",
            )
        elif (
            self.input_structure
            and on_localhost
            and len(self.input_structure.sites)
            > self.RUN_ON_LOCALHOST_NUM_SITES_WARN_THRESHOLD
        ):
            self._show_alert_message(
                "The selected code would be executed on the local host, but the "
                "number of sites of the selected structure is relatively large. "
                "Consider to select a code that runs on a larger system if "
                "necessary.",
                alert_class="warning",
            )

    @tl.observe("state")
    def _observe_state(self, change):
        with self.hold_trait_notifications():
            self.submit_button.disabled = change["new"] != self.State.CONFIGURED

    @tl.observe("previous_step_state")
    def _observe_input_structure(self, _):
        self._update_state()
        self.set_pdos_status()

    @tl.observe("process")
    def _observe_process(self, change):
        with self.hold_trait_notifications():
            process_node = change["new"]
            if process_node is not None:
                self.input_structure = process_node.inputs.structure
            self._update_state()

    def _on_submit_button_clicked(self, _):
        self.submit_button.disabled = True
        self.submit()

    def get_selected_codes(self):
        """Get the codes selected in the GUI.

        return: A dict with the code names as keys and the code UUIDs as values.
        """
        codes = {
            "pw": self.pw_code.value,
            "dos": self.dos_code.value,
            "projwfc": self.projwfc_code.value,
        }
        return codes

    def set_selected_codes(self, codes):
        """Set the inputs in the GUI based on a set of codes."""

        # Codes
        def _get_code_uuid(code):
            if code is not None:
                try:
                    return orm.load_code(code).uuid
                except NotExistent:
                    return None

        with self.hold_trait_notifications():
            # Codes
            self.pw_code.value = _get_code_uuid(codes["pw"])
            self.dos_code.value = _get_code_uuid(codes["dos"])
            self.projwfc_code.value = _get_code_uuid(codes["projwfc"])

    def set_pdos_status(self):
        if "pdos" in self.input_parameters.get("workchain", {}).get("properties", []):
            self.dos_code.code_select_dropdown.disabled = False
            self.projwfc_code.code_select_dropdown.disabled = False
        else:
            self.dos_code.code_select_dropdown.disabled = True
            self.projwfc_code.code_select_dropdown.disabled = True

    def submit(self, _=None):
        """Submit the work chain with the current inputs."""
        from aiida.orm.utils.serialize import serialize

        builder = self._create_builder()

        with self.hold_trait_notifications():
            process = submit(builder)

            process.label = self._generate_label()
            # since AiiDA data node may exist in the ui_parameters,
            # we serialize it to yaml
            process.base.extras.set("ui_parameters", serialize(self.ui_parameters))
            self.process = process

        self._update_state()

    def _generate_label(self) -> dict:
        """Generate a label for the work chain based on the input parameters."""
        formula = self.input_structure.get_formula()
        properties = [
            p for p in self.input_parameters["workchain"]["properties"] if p != "realx"
        ]
        relax_type = self.input_parameters["workchain"].get("relax_type")
        if relax_type != "none":
            relax_info = "structure is relaxed"
        else:
            relax_info = "structure is not relaxed"
        if not properties:
            properties_info = ""
        else:
            properties_info = f"properties on {', '.join(properties)}"

        label = "{} {} {}".format(formula, relax_info, properties_info)
        return label

    def _create_builder(self) -> ProcessBuilderNamespace:
        """Create the builder for the `QeAppWorkChain` submit."""
        from copy import deepcopy

        self.ui_parameters = deepcopy(self.input_parameters)
        self.ui_parameters["resources"] = self.get_resources()
        # add codes and resource info into ui_parameters
        self.ui_parameters.update(self.get_submission_parameters())
        builder = QeAppWorkChain.get_builder_from_protocol(
            structure=self.input_structure,
            parameters=deepcopy(self.ui_parameters),
        )

        self._update_builder(builder, self.MAX_MPI_PER_POOL)

        return builder

    def _update_builder(self, buildy, max_mpi_per_pool):
        resources = self.get_resources()
        npools = resources.pop("npools", 1)
        """Update the resources and parallelization of the ``QeAppWorkChain`` builder."""
        for k, v in buildy.items():
            if isinstance(v, (dict, ProcessBuilderNamespace)):
                if k == "pw" and v["pseudos"]:
                    v["parallelization"] = orm.Dict(dict={"npool": npools})
                if k == "projwfc":
                    v["settings"] = orm.Dict(dict={"cmdline": ["-nk", str(npools)]})
                if k == "dos":
                    v["metadata"]["options"]["resources"] = {
                        "num_machines": 1,
                        "num_mpiprocs_per_machine": min(
                            max_mpi_per_pool,
                            resources["num_mpiprocs_per_machine"],
                        ),
                    }
                    # Continue to the next item to avoid overriding the resources in the
                    # recursive `update_builder` call.
                    continue
                if k == "resources":
                    buildy["resources"] = resources
                else:
                    self._update_builder(v, max_mpi_per_pool)

    def set_submission_parameters(self, parameters):
        self.set_resources(parameters["resources"])
        self.set_selected_codes(parameters["codes"])

    def get_submission_parameters(self):
        return {
            "codes": self.get_selected_codes(),
            "resources": self.get_resources(),
        }

    def reset(self):
        with self.hold_trait_notifications():
            self.process = None
            self.input_structure = None
