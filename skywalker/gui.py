#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
from os import path
from functools import partial
from threading import RLock

import simplejson as json

from bluesky import RunEngine
from bluesky.utils import install_qt_kicker
from bluesky.preprocessors import run_wrapper, stage_wrapper

from pydm import Display
from pydm.PyQt.QtCore import (pyqtSlot, pyqtSignal,
                              QCoreApplication,
                              QObject, QEvent)
from pydm.PyQt.QtGui import QDoubleValidator, QDialog

from pcdsdevices.epics.attenuator import FeeAtt
from pswalker.plan_stubs import slit_scan_fiducialize
from pswalker.suspenders import (BeamEnergySuspendFloor,
                                 BeamRateSuspendFloor)
from pswalker.skywalker import skywalker

from skywalker.config import ConfigReader, SimConfigReader, sim_alignments
from skywalker.logger import GuiHandler
from skywalker.utils import ad_stats_x_axis_rot
from skywalker.settings import Setting, SettingsGroup
from skywalker.widgetgroup import (ObjWidgetGroup, ValueWidgetGroup,
                                   ImgObjWidget)

logger = logging.getLogger(__name__)
MAX_MIRRORS = 2


class SkywalkerGui(Display):
    """
    Display class to define all the logic for the skywalker alignment gui.
    Refers to widgets in the .ui file.

    Parameters
    ----------
    live : bool, optional
        Whether to launch application with live or simulated devices

    cfg : str, optional
        Configuration directory to use if not the default

    dark : bool, optional
        Choice to launch the application with a dark stylesheet

    parent : QWidget
        Parent Widget of application
    """
    def __init__(self, parent=None, live=False, cfg=None,  dark=True):
        super().__init__(parent=parent)
        ui = self.ui

        #Change the stylesheet
        if dark:
            try:
                import qdarkstyle
            except ImportError:
                logger.error("Can not use dark theme, "
                             "qdarkstyle package not available")
            else:
                self.setStyleSheet(qdarkstyle.load_stylesheet_pyqt5())

        # Configure debug file after all the qt logs
        logging.basicConfig(level=logging.DEBUG,
                            format=('%(asctime)s '
                                    '%(name)-12s '
                                    '%(levelname)-8s '
                                    '%(message)s'),
                            datefmt='%m-%d %H:%M:%S',
                            filename='./skywalker_debug.log',
                            filemode='a')

        # Set self.sim, self.loader, self.nominal_config
        self.sim = not live
        self.config_folder = cfg
        self.init_config()

        # Load things
        self.config_cache = {}
        self.cache_config()

        # Load system and alignments into the combo box objects
        ui.image_title_combo.clear()
        ui.procedure_combo.clear()
        ui.procedure_combo.addItem('None')
        self.all_imager_names = [entry['imager'] for entry in
                                 self.loader.live_systems.values()]
        for imager_name in self.all_imager_names:
            ui.image_title_combo.addItem(imager_name)
        for align in self.alignments.keys():
            ui.procedure_combo.addItem(align)

        # Pick out some initial parameters from system and alignment dicts
        first_system_key = list(self.alignments.values())[0][0][0]
        first_set = self.loader.get_subsystem(first_system_key)
        first_imager = first_set.get('imager', None)
        first_slit = first_set.get('slits', None)
        first_rotation = first_set.get('rotation', 0)

        # self.procedure and self.image_obj keep track of the gui state
        self.procedure = 'None'
        self.image_obj = first_imager

        # Initialize slit readback
        self.slit_group = ObjWidgetGroup([ui.slit_x_width,
                                          ui.slit_y_width,
                                          ui.slit_x_setpoint,
                                          ui.slit_y_setpoint,
                                          ui.slit_circle],
                                         ['xwidth.readback',
                                          'ywidth.readback',
                                          'xwidth.setpoint',
                                          'ywidth.setpoint',
                                          'xwidth.done'],
                                         first_slit,
                                         label=ui.readback_slits_title)

        # Initialize mirror control
        self.mirror_groups = []
        mirror_labels = self.get_widget_set('mirror_name')
        mirror_rbvs = self.get_widget_set('mirror_readback')
        mirror_vals = self.get_widget_set('mirror_setpos')
        mirror_circles = self.get_widget_set('mirror_circle')
        mirror_nominals = self.get_widget_set('move_nominal')
        for label, rbv, val, circle, nom, mirror in zip(mirror_labels,
                                                        mirror_rbvs,
                                                        mirror_vals,
                                                        mirror_circles,
                                                        mirror_nominals,
                                                        self.mirrors_padded()):
            mirror_group = ObjWidgetGroup([rbv, val, circle, nom],
                                          ['pitch.user_readback',
                                           'pitch.user_setpoint',
                                           'pitch.motor_done_move'],
                                          mirror, label=label)
            if mirror is None:
                mirror_group.hide()
            self.mirror_groups.append(mirror_group)

        # Initialize the goal entry fields
        self.goals_groups = []
        goal_labels = self.get_widget_set('goal_name')
        goal_edits = self.get_widget_set('goal_value')
        slit_checks = self.get_widget_set('slit_check')
        for label, edit, check, img, slit in zip(goal_labels, goal_edits,
                                                 slit_checks,
                                                 self.imagers_padded(),
                                                 self.slits_padded()):
            if img is None:
                name = None
            else:
                name = img.name
            validator = QDoubleValidator(0, 5000, 3)
            goal_group = ValueWidgetGroup(edit, label, checkbox=check,
                                          name=name, cache=self.config_cache,
                                          validator=validator)
            if img is None:
                goal_group.hide()
            elif slit is None:
                goal_group.checkbox.setEnabled(False)
            self.goals_groups.append(goal_group)

        # Initialize image and centroids. Needs goals defined first.
        self.image_group = ImgObjWidget(ui.image, first_imager,
                                        ui.beam_x_value, ui.beam_y_value,
                                        ui.beam_x_delta, ui.beam_y_delta,
                                        ui.image_state,
                                        ui.image_state_select,
                                        ui.readback_imager_title,
                                        self, first_rotation)
        ui.image.setColorMapToPreset('jet')

        # Initialize the settings window.
        first_step = Setting('first_step', 6.0)
        tolerance = Setting('tolerance', 5.0)
        averages = Setting('averages', 100)
        timeout = Setting('timeout', 600.0)
        tol_scaling = Setting('tol_scaling', 8.0)
        min_beam = Setting('min_beam', 1.0, required=False)
        min_rate = Setting('min_rate', 1.0, required=False)
        slit_width = Setting('slit_width', 0.2)
        samples = Setting('samples', 100)
        close_fee_att = Setting('close_fee_att', True)
        self.settings = SettingsGroup(
            parent=self,
            collumns=[['alignment'], ['slits', 'suspenders', 'setup']],
            alignment=[first_step, tolerance, averages, timeout, tol_scaling],
            suspenders=[min_beam, min_rate],
            slits=[slit_width, samples],
            setup=[close_fee_att])
        self.settings_cache = {}
        self.load_settings()
        self.restore_settings()
        self.cache_settings()  # Required in case nothing is loaded

        # Create the RunEngine that will be used in the alignments.
        # This gives us the ability to pause, etc.
        self.RE = RunEngine({})
        install_qt_kicker()

        # Some hax to keep the state string updated
        # There is probably a better way to do this
        # This might break on some package update
        self.RE.state  # Yes this matters
        old_set = RunEngine.state._memory[self.RE].set_
        def new_set(state):  # NOQA
            old_set(state)
            txt = " Status: " + state.capitalize()
            self.ui.status_label.setText(txt)
        RunEngine.state._memory[self.RE].set_ = new_set

        # Connect relevant signals and slots
        procedure_changed = ui.procedure_combo.currentIndexChanged[str]
        procedure_changed.connect(self.on_procedure_combo_changed)

        imager_changed = ui.image_title_combo.currentIndexChanged[str]
        imager_changed.connect(self.on_image_combo_changed)

        for goal_value in self.get_widget_set('goal_value'):
            goal_changed = goal_value.editingFinished
            goal_changed.connect(self.on_goal_changed)

        start_pressed = ui.start_button.clicked
        start_pressed.connect(self.on_start_button)

        pause_pressed = ui.pause_button.clicked
        pause_pressed.connect(self.on_pause_button)

        abort_pressed = ui.abort_button.clicked
        abort_pressed.connect(self.on_abort_button)

        slits_pressed = ui.slit_run_button.clicked
        slits_pressed.connect(self.on_slits_button)

        save_mirrors_pressed = ui.save_mirrors_button.clicked
        save_mirrors_pressed.connect(self.on_save_mirrors_button)

        save_goals_pressed = ui.save_goals_button.clicked
        save_goals_pressed.connect(self.on_save_goals_button)

        settings_pressed = ui.settings_button.clicked
        settings_pressed.connect(self.on_settings_button)

        for i, nominal_button in enumerate(mirror_nominals):
            nominal_pressed = nominal_button.clicked
            nominal_pressed.connect(partial(self.on_move_nominal_button, i))

        self.cam_lock = RLock()

        # Store some info about our screen size.
        QApp = QCoreApplication.instance()
        desktop = QApp.desktop()
        geometry = desktop.screenGeometry()
        self.screen_size = (geometry.width(), geometry.height())
        window_qsize = self.window().size()
        self.preferred_size = (window_qsize.width(), window_qsize.height())

        # Setup the post-init hook
        post_init = PostInit(self)
        self.installEventFilter(post_init)
        post_init.post_init.connect(self.on_post_init)

        # Setup the on-screen logger
        console = self.setup_gui_logger()

        # Stop the run if we get closed
        close_dict = dict(RE=self.RE, console=console)
        self.destroyed.connect(partial(SkywalkerGui.on_close, close_dict))

        # Put out the initialization message.
        init_base = 'Skywalker GUI initialized in '
        if self.sim:
            init_str = init_base + 'sim mode.'
        else:
            init_str = init_base + 'live mode.'
        logger.info(init_str)

    def init_config(self):
        if self.config_folder is None:
            this_dir = path.dirname(__file__)
            config_rel = path.join(this_dir, '..', 'config')
            self.config_folder = path.abspath(config_rel)
        self.nominal_config = self.get_cfg_path('nominal')
        self.happi_config = self.get_cfg_path('metadata')
        self.system_config = self.get_cfg_path('system')
        self.alignment_config = self.get_cfg_path('alignments')

        # Load files needed during __init__
        self.load_system()
        self.load_alignments()

    def get_cfg_path(self, name):
        if self.sim:
            name = 'sim_' + name
        return path.join(self.config_folder, name + '.json')

    def load_system(self):
        if self.sim:
            self.loader = SimConfigReader()
        else:
            self.loader = ConfigReader(self.happi_config, self.system_config)

    def load_alignments(self):
        if self.sim:
            self.alignments = sim_alignments
        else:
            with open(self.alignment_config, 'r') as f:
                d = json.load(f)
                self.alignments = d

    @pyqtSlot()
    def on_post_init(self):
        x = min(self.preferred_size[0], self.screen_size[0])
        y = min(self.preferred_size[1], self.screen_size[1])
        self.window().resize(x, y)

    # Close handler needs to be a static class method because it is run after
    # the object instance is already completely gone
    @staticmethod
    def on_close(close_dict):
        RE = close_dict['RE']
        console = close_dict['console']
        console.close()
        if RE.state != 'idle':
            RE.abort()

    def setup_gui_logger(self):
        """
        Initializes the text stream at the bottom of the gui. This text stream
        is actually just the log messages from Python!
        """
        console = GuiHandler(self.ui.log_text)
        console.setLevel(logging.INFO)
        formatter = logging.Formatter(fmt='%(asctime)s %(message)s',
                                      datefmt='%m-%d %H:%M:%S')
        console.setFormatter(formatter)
        logging.getLogger('').addHandler(console)
        return console

    @pyqtSlot(str)
    def on_image_combo_changed(self, imager_name):
        """
        Slot for the combo box above the image feed. This swaps out the imager,
        centroid, and slit readbacks.

        Parameters
        ----------
        imager_name: str
            name of the imager to activate
        """
        try:
            logger.info('Selecting imager %s', imager_name)
            systems = self.loader.get_systems_with(imager_name)
            if len(systems) == 0:
                logger.error('Invalid imager name.')
                return
            # Assume that imagers have exactly one slit and one rotation
            # Therefore, we can pick an arbitrary system entry that includes
            # the imager
            objs = self.loader.get_subsystem(systems[0])
            # This may have entries or may be missing entries if there was a
            # problem.
            try:
                image_obj = objs['imager']
                rotation = objs.get('rotation', 0)
                self.image_obj = image_obj
                self.image_group.change_obj(image_obj, rotation=rotation)
            except KeyError:
                logger.error('Failed to connect to imager')
            # Slits wasn't a mandatory field.
            slits_obj = objs.get('slits')
            if slits_obj is not None:
                self.slit_group.change_obj(slits_obj)
        except:
            logger.exception('Error on selecting imager')

    @pyqtSlot(str)
    def on_procedure_combo_changed(self, procedure_name):
        """
        Slot for the main procedure combo box. This swaps out the mirror and
        goals sections to match the chosen procedure, and determines what
        happens when we press go.

        Parameters
        ----------
        procedure_name: str
            name of the procedure to activate
        """
        try:
            logger.info('Selecting procedure %s', procedure_name)
            self.procedure = procedure_name
            if procedure_name == 'None':
                return
            else:
                self.load_active_system()
            for obj, widgets in zip(self.mirrors_padded(), self.mirror_groups):
                if obj is None:
                    widgets.hide()
                    widgets.change_obj(None)
                else:
                    widgets.change_obj(obj)
                    widgets.show()
            for obj, widgets in zip(self.imagers_padded(), self.goals_groups):
                widgets.save_value()
                widgets.clear()
            for obj, slit, widgets in zip(self.imagers_padded(),
                                          self.slits_padded(),
                                          self.goals_groups):
                if obj is None:
                    widgets.hide()
                else:
                    widgets.setup(name=obj.name)
                    if slit is None:
                        widgets.checkbox.setEnabled(False)
                    else:
                        widgets.checkbox.setEnabled(True)
                    widgets.show()
        except:
            logger.exception('Error on selecting procedure')

    @pyqtSlot()
    def on_goal_changed(self):
        """
        Slot for when the user picks a new goal. Updates the goal delta so it
        reflects the new chosen value.
        """
        try:
            self.image_group.update_deltas()
        except:
            logger.exception('Error on changing goal')

    @pyqtSlot()
    def on_start_button(self):
        """
        Slot for the start button. This begins from an idle state or resumes
        from a paused state.
        """
        try:
            if self.RE.state == 'idle':
                # Check for valid procedure
                if self.procedure == 'None':
                    logger.info("Please select a procedure.")
                    return

                # Check for valid goals
                active_size = len(self.active_system())
                raw_goals = []
                for i, goal in enumerate(self.goals()):
                    if i >= active_size:
                        break
                    elif goal is None:
                        msg = 'Please fill all goal fields before alignment.'
                        logger.info(msg)
                        return
                    raw_goals.append(goal)

                logger.info("Starting %s procedure with goals %s",
                            self.procedure, raw_goals)
                self.install_pick_cam()
                self.auto_switch_cam = True
                alignment = self.alignments[self.procedure]
                for key_set in alignment:
                    yags = [self.loader[key]['imager'] for key in key_set]
                    mots = [self.loader[key]['mirror'] for key in key_set]
                    rots = [self.loader[key].get('rotation')
                            for key in key_set]

                    # Make sure nominal positions are correct
                    for mot in mots:
                        try:
                            mot.nominal_position = self.config_cache[mot.name]
                        except KeyError:
                            pass

                    mot_rbv = 'pitch'
                    # We need to select det_rbv and interpret goals based on
                    # the camera rotation, converting things to the unrotated
                    # coordinates.
                    det_rbv = []
                    goals = []
                    for rot, yag, goal in zip(rots, yags, raw_goals):
                        rot_info = ad_stats_x_axis_rot(yag, rot)
                        det_rbv.append(rot_info['key'])
                        modifier = rot_info['mod_x']
                        if modifier is not None:
                            goal = modifier - goal
                        goals.append(goal)
                    first_steps = self.settings_cache['first_step']
                    tolerances = self.settings_cache['tolerance']
                    average = self.settings_cache['averages']
                    timeout = self.settings_cache['timeout']
                    tol_scaling = self.settings_cache['tol_scaling']

                    extra_stage = []
                    close_fee_att = self.settings_cache['close_fee_att']
                    if close_fee_att and not self.sim:
                        extra_stage.append(self.fee_att())

                    # Temporary fix: undo skywalker's goal mangling.
                    # TODO remove goal mangling from skywalker.
                    goals = [480 - g for g in goals]
                    plan = skywalker(yags, mots, det_rbv, mot_rbv, goals,
                                     first_steps=first_steps,
                                     tolerances=tolerances,
                                     averages=average, timeout=timeout,
                                     sim=self.sim, use_filters=not self.sim,
                                     tol_scaling=tol_scaling,
                                     extra_stage=extra_stage)
                    self.initialize_RE()
                    self.RE(plan)
            elif self.RE.state == 'paused':
                logger.info("Resuming procedure.")
                self.install_pick_cam()
                self.auto_switch_cam = True
                self.RE.resume()
        except:
            logger.exception('Error in running procedure')
        finally:
            self.auto_switch_cam = False

    @pyqtSlot()
    def on_pause_button(self):
        """
        Slot for the pause button. This brings us from the running state to the
        paused state.
        """
        self.auto_switch_cam = False
        if self.RE.state == 'running':
            logger.info("Pausing procedure.")
            try:
                self.RE.request_pause()
            except:
                logger.exception("Error on pause.")

    @pyqtSlot()
    def on_abort_button(self):
        """
        Slot for the abort button. This brings us from any state to the idle
        state.
        """
        self.auto_switch_cam = False
        if self.RE.state != 'idle':
            logger.info("Aborting procedure.")
            try:
                self.RE.abort()
            except:
                logger.exception("Error on abort.")

    @pyqtSlot()
    def on_slits_button(self):
        """
        Slot for the slits procedure. This checks the slit fiducialization.
        """
        try:
            logger.info('Starting slit check process.')
            image_to_check = []
            slits_to_check = []

            # First, check the slit checkboxes.
            for img_obj, slit_obj, goal_group in zip(self.imagers_padded(),
                                                     self.slits_padded(),
                                                     self.goals_groups):
                if slit_obj is not None and goal_group.is_checked:
                    image_to_check.append(img_obj)
                    slits_to_check.append(slit_obj)
            if not slits_to_check:
                logger.info('No valid slits selected!')
                return
            logger.info('Checking the following slits: %s',
                        [slit.name for slit in slits_to_check])

            self.install_pick_cam()
            self.auto_switch_cam = True

            slit_width = self.settings_cache['slit_width']
            samples = self.settings_cache['samples']

            def plan(img, slit, rot, output_obj, slit_width=slit_width,
                     samples=samples):
                rot_info = ad_stats_x_axis_rot(img, rot)
                det_rbv = rot_info['key']
                fidu = slit_scan_fiducialize(slit, img, centroid=det_rbv,
                                             x_width=slit_width,
                                             samples=samples)
                output = yield from fidu
                modifier = rot_info['mod_x']
                if modifier is not None:
                    output = modifier - output
                output_obj[img.name] = output

            self.initialize_RE()
            results = {}
            for img, slit in zip(image_to_check, slits_to_check):
                systems = self.loader.get_systems_with(img.name)
                objs = self.loader.get_subsystem(systems[0])
                rotation = objs.get('rotation', 0)
                this_plan = plan(img, slit, rotation, results)
                wrapped = run_wrapper(this_plan)
                wrapped = stage_wrapper(wrapped, [img, slit])
                self.RE(wrapped)

            logger.info('Slit scan found the following goals: %s', results)
            if self.ui.slit_fill_check.isChecked():
                logger.info('Filling goal fields automatically.')
                for img, fld in zip(self.imagers_padded(), self.goals_groups):
                    if img is not None:
                        try:
                            fld.value = round(results[img.name], 1)
                        except KeyError:
                            pass
        except:
            logger.exception('Error on slits button')
        finally:
            self.auto_switch_cam = False

    @pyqtSlot()
    def on_save_mirrors_button(self):
        try:
            if self.nominal_config is None:
                logger.info('No config file chosen.')
            else:
                logger.info('Saving mirror positions.')
                self.save_active_mirrors()
                self.cache_config()
        except:
            logger.exception('Error on saving mirrors')

    @pyqtSlot()
    def on_save_goals_button(self):
        try:
            logger.info('Saving goals.')
            self.save_active_goals()
            self.cache_config()
        except:
            logger.exception('Error on saving goals')

    @pyqtSlot()
    def on_settings_button(self):
        try:
            pos = self.ui.mapToGlobal(self.settings_button.pos())
            dialog_return = self.settings.dialog_at(pos)
            if dialog_return == QDialog.Accepted:
                self.cache_settings()
                self.save_settings()
                logger.info('Settings saved.')
            elif dialog_return == QDialog.Rejected:
                self.restore_settings()
                logger.info('Changes to settings cancelled.')
        except:
            logger.exception('Error on opening settings')

    @pyqtSlot(int)
    def on_move_nominal_button(self, index):
        try:
            nominal_positions = self.read_config() or {}
            try:
                mirror = self.mirrors()[index]
            except IndexError:
                logger.exception('Mirror index out of range')
                return
            try:
                pos = nominal_positions[mirror.name]
            except KeyError:
                logger.info('No mirror position saved')
                return
            logger.info('Moving %s to %s', mirror.name, pos)
            mirror.move(pos)
        except Exception:
            logger.exception('Misc error on move nominal button')

    def initialize_RE(self):
        """
        Set up the RunEngine for the current cached settings.
        """
        self.RE.clear_suspenders()
        min_beam = self.settings_cache['min_beam']
        min_rate = self.settings_cache['min_rate']
        if min_beam is not None:
            self.RE.install_suspender(BeamEnergySuspendFloor(min_beam, sleep=5,
                                                             averages=100))
        if min_rate is not None:
            self.RE.install_suspender(BeamRateSuspendFloor(min_rate, sleep=5))

    def fee_att(self):
        try:
            att = self._fee_att
        except AttributeError:
            att = FeeAtt()
            self._fee_att = att
        return att

    def cache_settings(self):
        """
        Pull settings from the settings object to the local cache.
        """
        self.settings_cache = self.settings.values

    def restore_settings(self):
        """
        Push settings from the local cache into the settings object.
        """
        self.settings.values = self.settings_cache

    def save_settings(self):
        """
        Write settings from the local cache to disk.
        """
        pass

    def load_settings(self):
        """
        Load settings from disk to the local cache.
        """
        pass

    def install_pick_cam(self):
        """
        For every camera that we've successfully loaded, subscribe the pick_cam
        method if we haven't done so already.
        """
        try:
            installed = self.installed
        except AttributeError:
            installed = set()
            self.installed = installed
        for system in self.loader.cache.values():
            imager = system['imager']
            if imager not in installed:
                imager.subscribe(self.pick_cam, event_type=imager.SUB_STATE,
                                 run=False)
                installed.add(imager)

    def pick_cam(self, *args, **kwargs):
        """
        Callback to switch the active imager as the procedures progress.
        """
        if self.auto_switch_cam:
            with self.cam_lock:
                chosen_imager = None
                for img in self.imagers():
                    pos = img.position
                    if pos == "Unknown":
                        return
                    elif pos == "IN":
                        chosen_imager = img
                        break
                combo = self.ui.image_title_combo
                if chosen_imager is not None:
                    name = chosen_imager.name
                    if name != combo.currentText():
                        logger.info('Automatically switching cam to %s', name)
                        index = self.all_imager_names.index(name)
                        combo.setCurrentIndex(index)

    def read_config(self):
        if self.nominal_config is not None:
            try:
                with open(self.nominal_config, 'r') as f:
                    d = json.load(f)
            except:
                return None
            return d
        return None

    def save_config(self, d):
        if self.nominal_config is not None:
            with open(self.nominal_config, 'w') as f:
                json.dump(d, f)

    def cache_config(self):
        d = self.read_config()
        if d is not None:
            self.config_cache.update(d)

    def save_goal(self, goal_group):
        if goal_group.value is None:
            logger.info('No value to save for this goal.')
            return
        d = self.read_config() or {}
        d[goal_group.text()] = goal_group.value
        self.save_config(d)

    def save_active_goals(self):
        text = []
        values = []
        for i, goal_group in enumerate(self.goals_groups):
            if i >= len(self.active_system()):
                break
            val = goal_group.value
            if val is not None:
                values.append(val)
                text.append(goal_group.text())
        d = self.read_config() or {}
        for t, v in zip(text, values):
            d[t] = v
        self.save_config(d)

    def save_mirror(self, mirror_group):
        d = self.read_config() or {}
        mirror = mirror_group.obj
        d[mirror.name] = mirror.position
        self.save_config(d)

    def save_active_mirrors(self):
        saves = {}
        averages = 1000
        all_mirrors = self.mirrors()
        for mirror in all_mirrors:
            saves[mirror.name] = 0
        for i in range(averages):
            for mirror in all_mirrors:
                saves[mirror.name] += mirror.position/averages
        logger.info('Saving positions: %s', saves)
        read = self.read_config() or {}
        read.update(saves)
        self.save_config(read)

    def active_system(self):
        """
        List of system keys that are part of the active procedure.
        """
        active_system = []
        if self.procedure != 'None':
            for part in self.alignments[self.procedure]:
                active_system.extend(part)
        return active_system

    def load_active_system(self):
        for system in self.active_system():
            self.loader.get_subsystem(system)

    def _objs(self, key):
        objs = []
        for act in self.active_system():
            subsystem = self.loader[act]
            if subsystem is None:
                objs.append(None)
            else:
                objs.append(subsystem[key])
        return objs

    def mirrors(self):
        """
        List of active mirror objects.
        """
        return self._objs('mirror')

    def imagers(self):
        """
        List of active imager objects.
        """
        return self._objs('imager')

    def slits(self):
        """
        List of active slits objects.
        """
        return self._objs('slits')

    def goals(self):
        """
        List of goals in the user entry boxes, or None for empty or invalid
        goals.
        """
        return [goal.value for goal in self.goals_groups]

    def goal(self):
        """
        The goal associated with the visible imager, or None if the visible
        imager is not part of the active procedure.
        """
        index = self.procedure_index()
        if index is None:
            return None
        else:
            return self.goals()[index]

    def procedure_index(self):
        """
        Goal index of the active imager, or None if the visible imager is not
        part of the active procedure.
        """
        try:
            return self.imagers_padded().index(self.image_obj)
        except ValueError:
            return None

    def none_pad(self, obj_list):
        """
        Helper function to extend a list with 'None' objects until it's the
        length of MAX_MIRRORS.
        """
        padded = []
        padded.extend(obj_list)
        while len(padded) < MAX_MIRRORS:
            padded.append(None)
        return padded

    def mirrors_padded(self):
        return self.none_pad(self.mirrors())

    def imagers_padded(self):
        return self.none_pad(self.imagers())

    def slits_padded(self):
        return self.none_pad(self.slits())

    def get_widget_set(self, name, num=MAX_MIRRORS):
        """
        Widgets that come in sets of count MAX_MIRRORS are named carefully so
        we can use this macro to grab related widgets.

        Parameters
        ----------
        name: str
            Base name of widget set e.g. 'name'

        num: int, optional
            Number of widgets to return

        Returns
        -------
        widget_set: list
            List of widgets e.g. 'name_1', 'name_2', 'name_3'...
        """
        widgets = []
        for n in range(1, num + 1):
            widget = getattr(self.ui, name + "_" + str(n))
            widgets.append(widget)
        return widgets

    def ui_filename(self):
        return 'gui.ui'

    def ui_filepath(self):
        return path.join(path.dirname(path.realpath(__file__)),
                         self.ui_filename())

intelclass = SkywalkerGui # NOQA


class PostInit(QObject):
    """
    Catch the visibility event for one last sequence of functions after pydm is
    fully initialized, which is later than we can do things inside __init__.
    """
    post_init = pyqtSignal()
    do_it = True

    def eventFilter(self, obj, event):
        if self.do_it and event.type() == QEvent.WindowActivate:
            self.do_it = False
            self.post_init.emit()
            return True
        return False
