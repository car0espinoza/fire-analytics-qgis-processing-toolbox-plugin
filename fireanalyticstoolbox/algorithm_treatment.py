# -*- coding: utf-8 -*-
"""
/***************************************************************************
 ProcessingPluginClass
                                 A QGIS plugin
 Description of the p p
 Generated by Plugin Builder: http://g-sherman.github.io/Qgis-Plugin-Builder/
                              -------------------
        begin                : 2024-03-20
        copyright            : (C) 2024 by fdo
        email                : fbadilla@ing.uchile.cl
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""
__author__ = "fdo"
__date__ = "2024-03-20"
__copyright__ = "(C) 2024 by fdo"
__version__ = "$Format:%H$"


from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from itertools import compress, product
from multiprocessing import cpu_count
from os import environ, pathsep
from pathlib import Path
from platform import system as platform_system
from shutil import which
from time import sleep

import numpy as np
import processing
from fire2a.raster import get_geotransform, get_rlayer_data, get_rlayer_info
from osgeo.gdal import GDT_Int16, GetDriverByName
from pandas import DataFrame, read_csv
from processing.tools.system import getTempFilename
from pyomo import environ as pyo
from pyomo.common.errors import ApplicationError
from pyomo.opt import SolverFactory, SolverStatus, TerminationCondition
from qgis.core import (Qgis, QgsFeature, QgsFeatureRequest, QgsFeatureSink, QgsField, QgsFields, QgsMessageLog,
                       QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingParameterBoolean,
                       QgsProcessingParameterDefinition, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterField, QgsProcessingParameterFile,
                       QgsProcessingParameterMultipleLayers, QgsProcessingParameterNumber,
                       QgsProcessingParameterRasterDestination, QgsProcessingParameterRasterLayer,
                       QgsProcessingParameterString)
from qgis.PyQt.QtCore import QByteArray, QCoreApplication, QVariant
from qgis.PyQt.QtGui import QIcon

from .algorithm_utils import (QgsProcessingParameterRasterDestinationGpkg, array2rasterInt16, get_output_raster_format,
                              get_raster_data, get_raster_info, get_raster_nodata, run_alg_style_raster_legend,
                              run_alg_styler_bin, write_log)
from .config import METRICS, NAME, SIM_OUTPUTS, STATS, TAG, jolo
from .decision_optimization.doop import (FileLikeFeedback, add_cbc_to_path, pyomo_init_algorithm, pyomo_parse_results,
                                         pyomo_run_model)


class RasterTreatmentAlgorithm(QgsProcessingAlgorithm):
    """Algorithm that selects the most valuable polygons restriced to a total weight using a MIP solver"""

    IN_TRT = "current_treatment"
    IN_VAL = "current_value"
    IN_TRGTS = "target_value"

    IN_TREATS = "treatments_costs"

    IN_AREA = "Area"
    IN_BUDGET = "Budget"

    OUT_LAYER = "OUT_LAYER"

    solver_exception_msg = ""

    if platform_system() == "Windows":
        add_cbc_to_path(QgsMessageLog)

    def initAlgorithm(self, config):
        """The form reads a vector layer and two fields, one for the value and one for the weight; also configures the weight ratio and the solver"""
        for raster in [self.IN_TRT, self.IN_VAL, self.IN_TRGTS]:
            self.addParameter(
                QgsProcessingParameterRasterLayer(
                    name=raster,
                    description=self.tr(f"Raster layer for {raster}"),
                    defaultValue=raster,
                    # defaultValue=[QgsProcessing.TypeRaster],
                    # optional=True,
                )
            )
        # treatments
        self.addParameter(
            QgsProcessingParameterFile(
                name=self.IN_TREATS,
                description=self.tr("Treatments Matrix (csv)"),
                behavior=QgsProcessingParameterFile.File,
                extension="csv",
            )
        )
        # AREA double
        qppn = QgsProcessingParameterNumber(
            name=self.IN_AREA,
            description=self.tr("Total Area"),
            type=QgsProcessingParameterNumber.Double,
            defaultValue=2024.03,
            optional=False,
            minValue=0.01,
        )
        qppn.setMetadata({"widget_wrapper": {"decimals": 2}})
        self.addParameter(qppn)
        # BUDGET double
        qppn = QgsProcessingParameterNumber(
            name=self.IN_BUDGET,
            description=self.tr("Total Budget"),
            type=QgsProcessingParameterNumber.Double,
            defaultValue=1312.01,
            optional=False,
            minValue=0.01,
        )
        qppn.setMetadata({"widget_wrapper": {"decimals": 2}})
        self.addParameter(qppn)

        # raster output
        self.addParameter(QgsProcessingParameterRasterDestinationGpkg(self.OUT_LAYER, self.tr("Raster Treatment")))

        pyomo_init_algorithm(self, config)

    def processAlgorithm(self, parameters, context, feedback):
        """
        TODO
            nodata != -1 -32768
            check px_area
        """
        instance = {}
        instance["nodata"] = -1
        # report solver unavailability
        feedback.pushWarning(f"Solver unavailability:\n{self.solver_exception_msg}\n")
        # read rasters
        rasters = {}
        for raster in [self.IN_TRT, self.IN_VAL, self.IN_TRGTS]:
            layer = self.parameterAsRasterLayer(parameters, raster, context)
            feedback.pushDebugInfo(f"{raster=}, {layer.publicSource()=}")
            rasters[raster] = {
                "layer": layer,
                "data": get_rlayer_data(layer),
                "info": get_rlayer_info(layer),
                "GT": get_geotransform(layer.publicSource()),
            }
            feedback.pushDebugInfo(f"{raster=}, {rasters[raster]['info']=}, {rasters[raster]['GT']=}")
        # feedback.pushDebugInfo(f"{rasters=}")
        instance["current_treatment"] = rasters[self.IN_TRT]["data"]
        instance["current_value"] = rasters[self.IN_VAL]["data"]
        instance["target_value"] = rasters[self.IN_TRGTS]["data"]
        TR, H, W = instance["target_value"].shape # fmt: skip
        # read conversion table
        df = read_csv(self.parameterAsFile(parameters, self.IN_TREATS, context), index_col=0)
        instance["treat_names"] = df.columns.values.tolist()
        if instance["treat_names"] != df.index.values.tolist():
            raise QgsProcessingException("Conversion table must be square with the same index and columns")
        if TR != len(instance["treat_names"]):
            raise QgsProcessingException(
                "Conversion table must have the same number of index an columns than target bands"
            )
        feedback.pushInfo(f"{instance['treat_names']=}")
        instance["treat_cost"] = df.values
        instance["px_area"] = rasters[self.IN_TRT]["info"]["cellsize_x"] * rasters[self.IN_TRT]["info"]["cellsize_y"]
        # feedback.pushDebugInfo(f"{df=}")

        instance["area"] = self.parameterAsDouble(parameters, self.IN_AREA, context)
        instance["budget"] = self.parameterAsDouble(parameters, self.IN_BUDGET, context)

        # feedback.pushDebugInfo(f"instance read: {instance=}")
        model = do_raster_treatment(**instance)
        results = pyomo_run_model(self, parameters, context, feedback, model, display_model=False)
        retval, solver_dic = pyomo_parse_results(results, feedback)

        retdic = {}
        retdic.update(solver_dic)

        if retval >= 1:
            retdic.update(instance)
            feedback.reportError(f"Solver failed with {retval=}")
            return retdic

        pyomo_std_feedback = FileLikeFeedback(feedback, True)
        pyomo_err_feedback = FileLikeFeedback(feedback, False)
        with redirect_stdout(pyomo_std_feedback), redirect_stderr(pyomo_err_feedback):
            model.area_capacity.display()
            model.budget_capacity.display()
            model.objective.display()

        treats_dic = {(h, w, tr): pyo.value(model.X[h, w, tr], exception=False) for h, w, tr in model.FeasibleMapTR}
        # feedback.pushDebugInfo(f"{treats_dic=}")
        treats_arr = np.array(
            [[treats_dic.get((h, w, tr), -3) for tr in model.TR] for h, w in np.ndindex(H, W)], dtype=float
        )
        treats_arr = np.where(np.isnan(treats_arr), -2, treats_arr)
        summary = np.array([np.where(np.any(row > 0), np.argmax(row), -1) for row in treats_arr])

        msg = "Solution histogram:\n"
        hist = np.histogram(summary, bins=[-3, -2, -1] + list(range(TR + 1)))[0]
        labels = ["unable", "undecided", "unchanged"] + instance["treat_names"]
        for trt, count in zip(labels, hist):
            msg += f"{trt}: {count}\n"
        feedback.pushInfo(msg)

        out_raster_filename = self.parameterAsOutputLayer(parameters, self.OUT_LAYER, context)
        out_raster_format = get_output_raster_format(out_raster_filename, feedback)
        ds = GetDriverByName(out_raster_format).Create(out_raster_filename, W, H, 1, GDT_Int16)
        ds.SetProjection(rasters["current_treatment"]["info"]["crs"].authid())  # export coords to file
        gt = rasters["current_treatment"]["GT"]
        if abs(gt[-1]) > 0:
            gt = (gt[0], gt[1], gt[2], gt[3], gt[4], -abs(gt[5]))
            feedback.pushWarning("Weird geotransform, Flipping Y axis...")
        feedback.pushDebugInfo(f"{gt=}")
        ds.SetGeoTransform(gt)  # specify coords
        band = ds.GetRasterBand(1)
        band.SetUnitType("treatment_index")
        if 0 != band.SetNoDataValue(int(-3)):
            feedback.pushWarning(f"Set No Data failed for {self.name()}")
        if 0 != band.WriteArray(summary.reshape(H, W)):
            feedback.pushWarning(f"WriteArray failed for {self.name()}")
        band = None
        ds.FlushCache()  # write to disk
        ds = None

        retdic[self.OUT_LAYER] = out_raster_filename

        if context.willLoadLayerOnCompletion(out_raster_filename):
            layer_details = context.layerToLoadOnCompletionDetails(out_raster_filename)
            layer_details.groupName = "DecisionOptimizationGroup"
            layer_details.name = "TreatmentRaster"
            layer_details.layerSortKey = 1
            context.layerToLoadOnCompletionDetails(out_raster_filename).setPostProcessor(
                run_alg_style_raster_legend(labels, offset=-3)
            )

        write_log(feedback, name=self.name())
        return retdic

    def name(self):
        """processing.run('provider:name',{..."""
        return "rastertreatment"

    def displayName(self):
        """
        Returns the translated algorithm name, which should be used for any
        user-visible display of the algorithm name.
        """
        return self.tr("Raster Treatment")

    def group(self):
        return self.tr(self.groupId())

    def groupId(self):
        return "do"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return RasterTreatmentAlgorithm()

    def helpUrl(self):
        return "https://www.github.com/fdobad/qgis-processingplugin-template/issues"

    def shortDescription(self):
        return self.tr(
            """<b>Objetive:</b> Maximize the changed value of the treated raster<br> 
            <b>Decisions:</b> Which treatment to apply to each pixel (or no change)<br>
            <b>Contraints:</b><br>
            (a) treat cost * pixel area less than budget<br>
            (b) treated area less than total area<br> 
            <b>Inputs:</b><br>
            (i) A .csv squared-table of <b>treatment transformation costs(/m2)</b> (defines index encoding)<br>
            (ii) A raster layer with <b>current treatments</b> index values (encoded: 0..number of treatments-1)<br>
            (iii) A raster layer with <b>current values</b><br>
            (iv) A multiband raster layer with <b>target values</b> (number of treatments == number of bands)<br>
            (v) A optional <b>boolean multiband raster</b> defining the allowed target treatments (1s is allowed)<br>
            (vi) <b>Budget</b> (same units than costs)<br>
            (vii) <b>Area</b> (same units than pixel size of the raster)<br>
            <br>
            - rasters "must be saved to disk" [gdal.Open(layer.publicSource(), gdal.GA_ReadOnly).GetGeoTransform() is used]<br>
            - consistency between rasters is up to the user<br>
            <br>
            sample: """
            + (Path(__file__).parent / "decision_optimization" / "treatments_sample").as_uri()
            + """<br><br> Use generate_polygon_treatment.py in QGIS's python console to generate a random instance (rasters & treatmets_costs.csv)"""
        )

    def icon(self):
        return QIcon(":/plugins/fireanalyticstoolbox/assets/firebreakmap.svg")


class PolyTreatmentAlgorithm(QgsProcessingAlgorithm):
    """Algorithm that selects the most valuable polygons restriced to a total weight using a MIP solver"""

    IN_LAYER = "IN_LAYER"
    IN_TRT = "treatment"
    IN_VAL = "value"
    IN_VALm2 = "value/m2"

    IN_TREATS = "TreatmentsTable"

    IN_AREA = "Area"
    IN_BUDGET = "Budget"

    OUT_LAYER = "OUT_LAYER"
    GEOMETRY_CHECK_SKIP_INVALID = "GEOMETRY_CHECK_SKIP_INVALID"

    solver_exception_msg = ""

    if platform_system() == "Windows":
        add_cbc_to_path(QgsMessageLog)

    def initAlgorithm(self, config):
        """The form reads a vector layer and two fields, one for the value and one for the weight; also configures the weight ratio and the solver"""
        # input layer
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                name=self.IN_LAYER,
                description=self.tr("Input Polygons Layer"),
                types=[QgsProcessing.TypeVectorPolygon],
            )
        )
        # current treatment field
        self.addParameter(
            QgsProcessingParameterField(
                name=self.IN_TRT,
                description=self.tr(f"Attribute table field name for {self.IN_TRT}"),
                defaultValue=self.IN_TRT,
                parentLayerParameterName=self.IN_LAYER,
                type=QgsProcessingParameterField.String,
                allowMultiple=False,
                optional=False,
                defaultToAllFields=False,
            )
        )
        # value & value/m2 field
        for field_value in [self.IN_VAL, self.IN_VALm2]:
            self.addParameter(
                QgsProcessingParameterField(
                    name=field_value,
                    description=self.tr(f"Attribute table field name for {field_value} [0s if not provided]"),
                    defaultValue=field_value,
                    parentLayerParameterName=self.IN_LAYER,
                    type=QgsProcessingParameterField.Numeric,
                    allowMultiple=False,
                    optional=True,
                    defaultToAllFields=False,
                )
            )
        # treatments
        self.addParameter(
            QgsProcessingParameterFile(
                name=self.IN_TREATS,
                description=self.tr("Treatments table (fid,treatment,value,value/m2,cost,cost/m2)"),
                behavior=QgsProcessingParameterFile.File,
                extension="csv",
            )
        )
        # AREA double
        qppn = QgsProcessingParameterNumber(
            name=self.IN_AREA,
            description=self.tr("Total Area"),
            type=QgsProcessingParameterNumber.Double,
            defaultValue=2024.03,
            optional=False,
            minValue=0.01,
            maxValue=999999999999,
        )
        qppn.setMetadata({"widget_wrapper": {"decimals": 2}})
        self.addParameter(qppn)
        # BUDGET double
        qppn = QgsProcessingParameterNumber(
            name=self.IN_BUDGET,
            description=self.tr("Total Budget"),
            type=QgsProcessingParameterNumber.Double,
            defaultValue=1312.01,
            optional=False,
            minValue=0.01,
        )
        qppn.setMetadata({"widget_wrapper": {"decimals": 2}})
        self.addParameter(qppn)

        # output layer
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUT_LAYER, self.tr("Polygon Treatment")))

        # advanced skip invalid geometry
        qppb = QgsProcessingParameterBoolean(
            name=self.GEOMETRY_CHECK_SKIP_INVALID,
            description=self.tr(
                "Set invalid geometry check to GeometrySkipInvalid (more options clicking the wrench on the input poly"
                " layer)"
            ),
            defaultValue=True,
            optional=True,
        )
        qppb.setFlags(qppb.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(qppb)

        pyomo_init_algorithm(self, config)

    def processAlgorithm(self, parameters, context, feedback):
        retdic = {}
        # report solver unavailability
        feedback.pushWarning(f"Solver unavailability:\n{self.solver_exception_msg}\n")
        # invalid geometry skip
        if self.parameterAsBool(parameters, self.GEOMETRY_CHECK_SKIP_INVALID, context):
            context.setInvalidGeometryCheck(QgsFeatureRequest.GeometrySkipInvalid)
            feedback.pushWarning("setInvalidGeometryCheck set to GeometrySkipInvalid")
        # poly layer
        layer = self.parameterAsSource(parameters, self.IN_LAYER, context)
        feedback.pushDebugInfo(
            f"{layer.sourceName()=}, {layer.fields().names()=}, {layer.wkbType()=}, {layer.sourceCrs().authid()=},"
            f" {layer.featureCount()=}"
        )
        retdic["layer"] = layer
        # fields
        request_fields = []
        # required
        current_treatment_fieldname = self.parameterAsString(parameters, self.IN_TRT, context)
        request_fields += [current_treatment_fieldname]
        # optional
        if current_value_fieldname := self.parameterAsString(parameters, self.IN_VAL, context):
            request_fields += [current_value_fieldname]
        if current_valuem2_fieldname := self.parameterAsString(parameters, self.IN_VALm2, context):
            request_fields += [current_valuem2_fieldname]
        qfr = QgsFeatureRequest().setSubsetOfAttributes(request_fields, layer.fields())
        features = list(layer.getFeatures(qfr))
        feedback.pushWarning(
            f"Valid polygons: {len(features)}/{layer.featureCount()} {len(features)/layer.featureCount():.2%}\n"
        )
        # required
        current_treatment = [feat.attribute(current_treatment_fieldname) for feat in features]
        fids = [feat.id() for feat in features]
        # get else not provided
        if current_value_fieldname:
            current_value = [feat.attribute(current_value_fieldname) for feat in features]
        else:
            current_value = [0] * len(features)
        if current_valuem2_fieldname:
            current_valuem2 = [feat.attribute(current_valuem2_fieldname) for feat in features]
        else:
            current_valuem2 = [0] * len(features)
        attr_names = ["fid", "treatment", "value", "value/m2", "area"]
        dfa = DataFrame.from_dict(
            dict(
                zip(
                    attr_names,
                    [
                        fids,
                        current_treatment,
                        current_value,
                        current_valuem2,
                        [feat.geometry().area() for feat in features],
                    ],
                )
            )
        )
        # feedback.pushDebugInfo(dfa)
        retdic["dfa"] = dfa
        # read tables
        dft = read_csv(self.parameterAsFile(parameters, self.IN_TREATS, context))
        for col in ["fid", "treatment", "value", "value/m2", "cost", "cost/m2"]:
            if col not in dft.columns:
                raise QgsProcessingException(f"Column {col} not found in {dft.columns}")
        # feedback.pushDebugInfo(dft)
        retdic["dft"] = dft

        budget = self.parameterAsDouble(parameters, self.IN_BUDGET, context)
        area = self.parameterAsDouble(parameters, self.IN_AREA, context)

        treat_names = np.unique(dft["treatment"].to_list() + current_treatment).tolist()
        # feedback.pushDebugInfo(f"{treat_names=}")
        retdic["treat_names"] = treat_names

        treat_table = np.zeros((len(dfa), len(treat_names)), dtype=bool)
        for i, current in dfa.iterrows():
            targets = dft[dft["fid"] == current["fid"]]
            for j, target in targets.iterrows():
                treat_table[i, treat_names.index(target["treatment"])] = True
        # feedback.pushDebugInfo(f"{treat_table=}")
        retdic["treat_table"] = treat_table

        # feedback.pushDebugInfo(f"instance read: {retdic=}")
        model = do_poly_treatment(treat_names, treat_table, dfa, dft, area, budget)
        results = pyomo_run_model(self, parameters, context, feedback, model)
        retval, solver_dic = pyomo_parse_results(results, feedback)
        retdic.update(solver_dic)

        if retval >= 1:
            return retdic

        # feedback.pushDebugInfo(f"{treat_names=}")
        treats_dic = {(i, k): pyo.value(model.X[i, k], exception=False) for i, k in model.FeasibleSet}
        # feedback.pushDebugInfo(f"{treats_dic=}")
        treats_arr = np.array([[treats_dic.get((i, k)) for k in model.T] for i in model.N], dtype=float)
        treats_arr = np.where(np.isnan(treats_arr), -1, treats_arr)
        summary = np.array([np.max(row) for row in treats_arr])
        # feedback.pushDebugInfo(f"{treats_dic=}")
        # feedback.pushDebugInfo(f"{list(zip(treats_arr,summary))=}")

        msg = "Solution histogram:\n"
        hist = np.histogram(summary, bins=[-1] + list(range(len(treat_names))))[0]
        for trt, count in zip(["undecided"] + treat_names, hist):
            msg += f"{trt}: {count}\n"
        feedback.pushInfo(msg)

        fields = QgsFields()
        fields.append(QgsField(name="fid", type=QVariant.Int))  # , len=10))
        fields.append(QgsField(name="current", type=QVariant.String))  # , len=10))
        fields.append(QgsField(name="treatment", type=QVariant.String))  # , len=10))
        fields.append(QgsField(name="changed", type=QVariant.Bool))  # , len=10))

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUT_LAYER,
            context,
            fields,
            layer.wkbType(),
            layer.sourceCrs(),
        )
        feedback.pushDebugInfo(f"{sink=}, {dest_id=}")

        total = 100.0 / len(features)
        for current, feat in enumerate(features):
            # Stop the algorithm if cancel button has been clicked
            if feedback.isCanceled():
                break
            # Prepare feature
            new_feat = QgsFeature(fields)
            ifid = int(feat.id())
            curr = feat[current_treatment_fieldname]

            smry = summary[fids.index(ifid)]
            if smry == -1:
                trgt = "undecided"
                chg = True
            elif smry == 0:
                trgt = ""
                chg = False
            elif smry == 1:
                trgt = treat_names[np.argmax(treats_arr[fids.index(ifid)])]
                chg = True
            else:
                feedback.reportError(f"Unexpected summary value: {smry}, for feature {ifid}")

            new_feat.setId(ifid)
            new_feat.setAttributes([ifid, curr, trgt, chg])
            new_feat.setGeometry(feat.geometry())
            # feedback.pushDebugInfo(
            #     f"{current=}, {new_feat.id()=}, {[treats_dic.get((ifid, tn)) for tn in treat_names]}"
            # )
            # Add a feature in the sink
            sink.addFeature(new_feat, QgsFeatureSink.FastInsert)
            # Update the progress bar
            feedback.setProgress(int(current * total))

        # if showing
        if context.willLoadLayerOnCompletion(dest_id):
            layer_details = context.layerToLoadOnCompletionDetails(dest_id)
            layer_details.groupName = "DecisionOptimizationGroup"
            layer_details.name = "TreatmentPolygons"
            layer_details.layerSortKey = 1
            processing.run(
                "native:setlayerstyle",
                {
                    "INPUT": dest_id,
                    "STYLE": str(Path(__file__).parent / "decision_optimization" / "treatment_polygon.qml"),
                },
                context=context,
                feedback=feedback,
                is_child_algorithm=True,
            )

        write_log(feedback, name=self.name())
        return retdic

    def name(self):
        """processing.run('provider:name',{..."""
        return "polytreatment"

    def displayName(self):
        """
        Returns the translated algorithm name, which should be used for any
        user-visible display of the algorithm name.
        """
        return self.tr("Polygon Treatment")

    def group(self):
        return self.tr("Decision Optimization")

    def groupId(self):
        return "do"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return PolyTreatmentAlgorithm()

    def helpUrl(self):
        return "https://www.github.com/fdobad/qgis-processingplugin-template/issues"

    def shortDescription(self):
        return self.tr(
            """<b>Objetive:</b> Maximize the changed value of the treated polygons<br> 
            <b>Decisions:</b> Which treatment to apply to each polygon (or no change)<br>
            <b>Contraints:</b><br>
            (a) fixed+area costs less than budget<br>
            (b) treated area less than total area<br> 
            <b>Inputs:</b><br>
            (i) A polygon layer with <b>current</b> attributes: [fid],<b>treatment, value, value/m2</b><br>
            (ii) A .csv table defining <b>target</b> treatments: <b>fid, treatment, value, value/m2, cost, cost/m2</b> (use these column names)<br>
            - fid is the feature id of each polygon so it's given in the attribute table, but must be specified in the .csv table<br>
            - current & target treatment are just strings, but each polygon needs at least one feasible treatment (one row)<br>
            - current & target values[/m2] weight towards the objective when no change (keep current) or a target treatment is recommended<br>
            (iii) <b>Budget</b> (same units than costs)<br>
            (iv) <b>Area</b> (same units than the geometry of the polygons)<br>
            <br>
            <br>
            Sample: """
            + (Path(__file__).parent / "decision_optimization" / "treatments_sample").as_uri()
            + """<br><br> Use polygons.gpkg, polygons_treatments.csv & polygons_params.txt to run a sample<br><br>
            Or generate_polygon_treatment.py in QGIS's python console to generate a sensible instance in any polygon layer"""
        )

    def icon(self):
        return QIcon(":/plugins/fireanalyticstoolbox/assets/firebreakmap.svg")


def do_raster_treatment(
    nodata, treat_names, treat_cost, current_treatment, current_value, target_value, px_area, area, budget
):
    # Integer Programming
    m = pyo.ConcreteModel(name="raster_treatment")

    H, W = current_value.shape
    assert len(treat_names) == target_value.shape[0]

    current_value_nodata = list(zip(*np.where(current_value == nodata)))
    current_treatment_nodata = list(zip(*np.where(current_treatment == nodata)))
    target_value_nodata = [(h, w) for h, w in np.ndindex(H, W) if np.all(target_value[:, h, w] == nodata)]
    # print(f"{current_value_nodata=}, {current_treatment_nodata=}, {target_value_nodata=}")
    nodata_idxs = set(current_value_nodata + current_treatment_nodata + target_value_nodata)
    # print(f"{nodata_idxs=}")

    # Sets
    m.H = pyo.Set(initialize=range(H))
    m.W = pyo.Set(initialize=range(W))
    m.TR = pyo.Set(initialize=treat_names)

    # list indices of H,W not in nodata_idx
    m.FeasibleMap = pyo.Set(initialize=[(h, w) for h, w in product(m.H, m.W) if (h, w) not in nodata_idxs])
    m.FeasibleMapTR = pyo.Set(
        initialize=[
            (h, w, tr)
            for h, w, tr in product(m.H, m.W, m.TR)
            if (h, w) not in nodata_idxs and treat_names[current_treatment[h, w]] != tr
        ]
    )

    # Params
    # 1d
    m.px_area = pyo.Param(within=pyo.Reals, initialize=px_area)
    m.area = pyo.Param(within=pyo.Reals, initialize=area)
    m.budget = pyo.Param(within=pyo.Reals, initialize=budget)
    # 2d
    m.current_value = pyo.Param(
        m.FeasibleMap,
        within=pyo.Reals,
        initialize={(h, w): current_value[h, w] for h, w in m.FeasibleMap},
    )
    m.treat_cost = pyo.Param(
        m.TR,
        m.TR,
        within=pyo.Reals,
        initialize={
            (treat_names[tr1], treat_names[tr2]): val for (tr1, tr2), val in np.ndenumerate(treat_cost) if tr1 != tr2
        },
    )
    # 3d
    m.target_value = pyo.Param(
        m.FeasibleMapTR,
        within=pyo.Reals,
        initialize={(h, w, tr): target_value[treat_names.index(tr), h, w] for h, w, tr in m.FeasibleMapTR},
    )

    # Variables
    m.X = pyo.Var(
        m.FeasibleMapTR,
        within=pyo.Binary,
    )

    # Constraints
    m.sos_at_most_one_treatment = pyo.SOSConstraint(
        m.FeasibleMap,
        sos=1,
        rule=lambda m, hh, ww: [m.X[h, w, tr] for h, w, tr in m.FeasibleMapTR if h == hh and w == ww],
    )

    m.area_capacity = pyo.Constraint(
        rule=lambda m: sum(m.X[h, w, tr] * m.px_area for h, w, tr in m.FeasibleMapTR) <= m.area
    )

    m.budget_capacity = pyo.Constraint(
        rule=lambda m: sum(
            m.X[h, w, tr] * m.treat_cost[treat_names[current_treatment[h, w]], tr] * m.px_area
            for h, w, tr in m.FeasibleMapTR
        )
        <= m.budget
    )

    # Objective
    m.objective = pyo.Objective(
        expr=sum(
            m.X[h, w, tr] * (m.target_value[h, w, tr] * m.px_area)
            + (1 - m.X[h, w, tr]) * (m.current_value[h, w] * m.px_area)
            for h, w, tr in m.FeasibleMapTR
        ),
        sense=pyo.maximize,
    )

    return m


def do_poly_treatment(treat_names, treat_table, dfa, dft, area, budget):
    """Integer Programming
    Hints:
     initialize=df_stands[treatments].stack().to_dict(),
    TODO
     + dfa[dfa.fid == i].index[0] -> dfa.set_index("fid").loc[i] ?
    """
    m = pyo.ConcreteModel(name="polygon_treatment")

    # Sets
    m.N = pyo.Set(initialize=dfa.fid, ordered=True)
    m.T = pyo.Set(initialize=treat_names)
    m.FeasibleSet = pyo.Set(
        initialize=[
            (i, k) for i, k in product(m.N, m.T) if treat_table[dfa[dfa.fid == i].index[0], treat_names.index(k)]
        ]
    )
    dfa.set_index("fid", inplace=True)

    # Params
    m.Area = pyo.Param(within=pyo.Reals, initialize=area)
    m.Budget = pyo.Param(within=pyo.Reals, initialize=budget)
    # 1d
    m.area = pyo.Param(m.N, within=pyo.Reals, initialize=dfa["area"].to_dict())
    m.current_value = pyo.Param(m.N, within=pyo.Reals, initialize=dfa["value"].to_dict())
    m.current_valuem2 = pyo.Param(m.N, within=pyo.Reals, initialize=dfa["value/m2"].to_dict())
    m.target_value = pyo.Param(
        m.N, m.T, within=pyo.Reals, initialize=dft.set_index(["fid", "treatment"])["value"].to_dict()
    )
    m.target_valuem2 = pyo.Param(
        m.N, m.T, within=pyo.Reals, initialize=dft.set_index(["fid", "treatment"])["value/m2"].to_dict()
    )
    m.cost = pyo.Param(m.N, m.T, within=pyo.Reals, initialize=dft.set_index(["fid", "treatment"])["cost"].to_dict())
    m.costm2 = pyo.Param(
        m.N, m.T, within=pyo.Reals, initialize=dft.set_index(["fid", "treatment"])["cost/m2"].to_dict()
    )

    # Variables
    m.X = pyo.Var(
        m.FeasibleSet,
        within=pyo.Binary,
    )
    # Constraints
    m.sos_at_most_one_treatment = pyo.SOSConstraint(
        m.N, sos=1, rule=lambda m, ii: [m.X[i, k] for i, k in m.FeasibleSet if i == ii]
    )

    m.area_capacity = pyo.Constraint(rule=lambda m: sum(m.X[i, k] * m.area[i] for i, k in m.FeasibleSet) <= m.Area)

    m.budget_capacity = pyo.Constraint(
        rule=lambda m: sum(m.X[i, k] * (m.cost[i, k] + m.costm2[i, k] * m.area[i]) for i, k in m.FeasibleSet)
        <= m.Budget
    )

    # Objective
    m.objective = pyo.Objective(
        expr=sum(
            m.X[i, k] * (m.target_value[i, k] + m.target_valuem2[i, k] * m.area[i])
            + (1 - m.X[i, k]) * (m.current_value[i] + m.current_valuem2[i] * m.area[i])
            for i, k in m.FeasibleSet
        ),
        sense=pyo.maximize,
    )

    return m
