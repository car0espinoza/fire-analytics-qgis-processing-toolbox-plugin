# -*- coding: utf-8 -*-
"""
/***************************************************************************
 ProcessingPluginClass
                                 A QGIS plugin
 Description of the p p
 Generated by Plugin Builder: http://g-sherman.github.io/Qgis-Plugin-Builder/
                              -------------------
        begin                : 2023-07-12
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
__date__ = "2024-03-01"
__copyright__ = "(C) 2024 by fdo"
__version__ = "$Format:%H$"


from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from platform import system as platform_system
from time import sleep

import numpy as np
import processing
from processing.tools.system import getTempFilename
from pyomo import environ as pyo
from pyomo.common.errors import ApplicationError
from pyomo.opt import SolverFactory, SolverStatus, TerminationCondition
from qgis.core import (Qgis, QgsFeature, QgsFeatureRequest, QgsFeatureSink, QgsField, QgsFields, QgsMessageLog,
                       QgsProcessing, QgsProcessingAlgorithm, QgsProcessingParameterBoolean,
                       QgsProcessingParameterDefinition, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterField, QgsProcessingParameterFile,
                       QgsProcessingParameterNumber, QgsProcessingParameterRasterDestination,
                       QgsProcessingParameterRasterLayer, QgsProcessingParameterString)
from qgis.PyQt.QtCore import QByteArray, QCoreApplication, QVariant
from qgis.PyQt.QtGui import QIcon
from scipy import stats

from ..algorithm_utils import (QgsProcessingParameterRasterDestinationGpkg, array2rasterInt16, get_output_raster_format,
                               get_raster_data, get_raster_info, get_raster_nodata, run_alg_styler_bin, write_log)
from ..config import METRICS, NAME, SIM_OUTPUTS, STATS, TAG, jolo
from .doop import SOLVER, FileLikeFeedback, add_cbc_to_path, check_solver_availability


class PolygonKnapsackAlgorithm(QgsProcessingAlgorithm):
    """Algorithm that selects the most valuable polygons restriced to a total weight using a MIP solver"""

    IN_LAYER = "IN_LAYER"
    IN_VALUE = "VALUE"
    IN_WEIGHT = "WEIGHT"
    IN_RATIO = "RATIO"
    IN_EXECUTABLE = "EXECUTABLE"
    OUT_LAYER = "OUT_LAYER"
    GEOMETRY_CHECK_SKIP_INVALID = "GEOMETRY_CHECK_SKIP_INVALID"

    solver_exception_msg = ""

    if platform_system() == "Windows":
        add_cbc_to_path()

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
        # value field
        self.addParameter(
            QgsProcessingParameterField(
                name=self.IN_VALUE,
                description=self.tr("Attribute table field name for VALUE (if blank 1's will be used)"),
                defaultValue="VALUE",
                parentLayerParameterName=self.IN_LAYER,
                type=Qgis.ProcessingFieldParameterDataType.Numeric,
                allowMultiple=False,
                optional=True,
                defaultToAllFields=False,
            )
        )
        # weight field
        self.addParameter(
            QgsProcessingParameterField(
                name=self.IN_WEIGHT,
                description=self.tr("Attribute table field name for WEIGHT (if blank polygon's area will be used)"),
                defaultValue="WEIGHT",
                parentLayerParameterName=self.IN_LAYER,
                type=Qgis.ProcessingFieldParameterDataType.Numeric,
                allowMultiple=False,
                optional=True,
                defaultToAllFields=False,
            )
        )
        # ratio double
        qppn = QgsProcessingParameterNumber(
            name=self.IN_RATIO,
            description=self.tr("Capacity ratio (1 = weight.sum)"),
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.069,
            optional=False,
            minValue=0.0,
            maxValue=1.0,
        )
        qppn.setMetadata({"widget_wrapper": {"decimals": 3}})
        self.addParameter(qppn)
        # output layer
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUT_LAYER, self.tr("Polygon Knapsack Output Layer")))
        # SOLVERS
        value_hints, self.solver_exception_msg = check_solver_availability(SOLVER)
        # solver string combobox (enums
        qpps = QgsProcessingParameterString(
            name="SOLVER",
            description="Solver: recommended options string [and executable STATUS]",
        )
        qpps.setMetadata(
            {
                "widget_wrapper": {
                    "value_hints": value_hints,
                    "setEditable": True,  # not working
                }
            }
        )
        qpps.setFlags(qpps.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(qpps)
        # options_string
        qpps2 = QgsProcessingParameterString(
            name="CUSTOM_OPTIONS_STRING",
            description="Override options_string (type a single space ' ' to not send any options to the solver)",
            defaultValue="",
            optional=True,
        )
        qpps2.setFlags(qpps2.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(qpps2)
        # executable file
        qppf = QgsProcessingParameterFile(
            name=self.IN_EXECUTABLE,
            description=self.tr("Set solver executable file [REQUIRED if STATUS]"),
            behavior=QgsProcessingParameterFile.File,
            extension="exe" if platform_system() == "Windows" else "",
            optional=True,
        )
        qppf.setFlags(qppf.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(qppf)

        qppb = QgsProcessingParameterBoolean(
            name=self.GEOMETRY_CHECK_SKIP_INVALID,
            description=self.tr(
                "Set invalid geometry check to GeometrySkipInvalid (more options clicking the wrench on the input poly layer)"
            ),
            defaultValue=True,
            optional=True,
        )
        qppb.setFlags(qppb.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(qppb)

    def processAlgorithm(self, parameters, context, feedback):
        # setup ignore
        if self.parameterAsBool(parameters, self.GEOMETRY_CHECK_SKIP_INVALID, context):
            context.setInvalidGeometryCheck(QgsFeatureRequest.GeometrySkipInvalid)
            feedback.pushWarning("setInvalidGeometryCheck set to GeometrySkipInvalid")

        # report solver unavailability
        feedback.pushWarning(f"Solver unavailability:\n{self.solver_exception_msg}\n")

        layer = self.parameterAsSource(parameters, self.IN_LAYER, context)
        feedback.pushDebugInfo(
            f"{layer=}, {layer.fields()=}, {layer.wkbType()=}, {layer.sourceCrs()=}, {layer.featureCount()=}"
        )

        request_fields = []
        if value_fieldname := self.parameterAsString(parameters, self.IN_VALUE, context):
            request_fields += [value_fieldname]
        if weight_fieldname := self.parameterAsString(parameters, self.IN_WEIGHT, context):
            request_fields += [weight_fieldname]
        qfr = QgsFeatureRequest().setSubsetOfAttributes(request_fields, layer.fields())
        features = list(layer.getFeatures(qfr))
        feedback.pushWarning(
            f"Valid polygons: {len(features)}/{layer.featureCount()} {len(features)/layer.featureCount():.2%}\n"
        )

        if value_fieldname:
            value_data = [feat.attribute(value_fieldname) for feat in features]
        else:
            value_data = [1] * len(features)
            feedback.pushWarning("No value field, using 1's")
        value_data = np.array(value_data)

        if weight_fieldname:
            weight_data = [feat.attribute(weight_fieldname) for feat in features]
        else:
            weight_data = [feat.geometry().area() for feat in features]
            feedback.pushWarning("No weight field, using polygon areas")
        weight_data = np.array(weight_data)

        feedback.pushDebugInfo(f"{value_data.shape=}, {value_data=}\n{weight_data.shape=}, {weight_data=}\n")

        assert len(value_data) == len(weight_data)
        N = len(value_data)
        no_indexes = np.where(np.isnan(value_data) | np.isnan(weight_data))[0]
        feedback.pushWarning(
            f"discarded polygons (value or weight invalid): {len(no_indexes)}/{N} {len(no_indexes)/N:.2%}\n"
        )

        response, status, termCondition = do_knapsack(
            self, value_data, weight_data, no_indexes, feedback, parameters, context
        )
        feedback.pushDebugInfo(f"{response=}, {response.shape=}")
        # response[response == None] = -2
        assert N == len(response)

        undecided, skipped, not_selected, selected = np.histogram(response, bins=[-2, -1, 0, 1, 2])[0]
        feedback.pushInfo(
            "Solution histogram:\n"
            f"{selected=}\n"
            f"{not_selected=}\n"
            f"{skipped=} (invalid value or weight)\n"
            f"{undecided=}\n"
        )

        fields = QgsFields()
        fields.append(QgsField(name="fid", type=QVariant.Int))  # , len=10))
        fields.append(QgsField(name="knapsack", type=QVariant.Int))  # , len=10))

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUT_LAYER,
            context,
            fields,
            layer.wkbType(),
            layer.sourceCrs(),
        )
        feedback.pushDebugInfo(f"{sink=}, {dest_id=}")

        total = 100.0 / N
        for current, feature in enumerate(features):
            # Stop the algorithm if cancel button has been clicked
            if feedback.isCanceled():
                break
            # Prepare feature
            new_feature = QgsFeature(fields)
            fid = int(feature.id())
            res = int(response[current])
            new_feature.setId(fid)
            new_feature.setAttributes([fid, res])
            new_feature.setGeometry(feature.geometry())
            # feedback.pushDebugInfo(f"{new_feature.id()=}, {current=}, {response[current]=}")
            # Add a feature in the sink
            sink.addFeature(new_feature, QgsFeatureSink.FastInsert)
            # Update the progress bar
            feedback.setProgress(int(current * total))

        # if showing
        if context.willLoadLayerOnCompletion(dest_id):
            layer_details = context.layerToLoadOnCompletionDetails(dest_id)
            layer_details.groupName = "DecisionOptimizationGroup"
            layer_details.name = "KnapsackPolygons"
            # layer_details.layerSortKey = 2
            processing.run(
                "native:setlayerstyle",
                {
                    "INPUT": dest_id,
                    "STYLE": str(Path(__file__).parent / "knapsack_polygon.qml"),
                },
                context=context,
                feedback=feedback,
                is_child_algorithm=True,
            )

        write_log(feedback, name=self.name())
        return {
            self.OUT_LAYER: dest_id,
            "SOLVER_STATUS": status,
            "SOLVER_TERMINATION_CONDITION": termCondition,
        }

    def name(self):
        """processing.run('provider:name',{..."""
        return "polygonknapsack"

    def displayName(self):
        """
        Returns the translated algorithm name, which should be used for any
        user-visible display of the algorithm name.
        """
        return self.tr("Polygon Knapsack")

    def group(self):
        return self.tr("Decision Optimization")

    def groupId(self):
        return "do"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return PolygonKnapsackAlgorithm()

    def helpUrl(self):
        return "https://www.github.com/fdobad/qgis-processingplugin-template/issues"

    def shortDescription(self):
        return self.tr(
            """Optimizes the classical knapsack problem using polygons with values and/or weights attributes, returns a polygon layer with the selected polygons."""
        )

    def icon(self):
        return QIcon(":/plugins/fireanalyticstoolbox/assets/firebreakmap.svg")


class RasterKnapsackAlgorithm(QgsProcessingAlgorithm):
    """Algorithm that takes selects the most valuable raster pixels restriced to a total weight using a MIP solver"""

    IN_VALUE = "VALUE"
    IN_WEIGHT = "WEIGHT"
    IN_RATIO = "RATIO"
    IN_EXECUTABLE = "EXECUTABLE"
    OUT_LAYER = "OUT_LAYER"

    NODATA = -32768  # -1?
    solver_exception_msg = ""

    if platform_system() == "Windows":
        add_cbc_to_path()

    def initAlgorithm(self, config):
        """The form reads two raster layers one for the value and one for the weight; also configures the weight ratio and the solver"""
        # value raster
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                name=self.IN_VALUE,
                description=self.tr("Values layer (if blank 1's will be used)"),
                defaultValue=[QgsProcessing.TypeRaster],
                optional=True,
            )
        )
        # weight raster
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                name=self.IN_WEIGHT,
                description=self.tr("Weights layer (if blank 1's will be used)"),
                defaultValue=[QgsProcessing.TypeRaster],
                optional=True,
            )
        )
        # ratio double
        qppn = QgsProcessingParameterNumber(
            name=self.IN_RATIO,
            description=self.tr("Capacity ratio (1 = weight.sum)"),
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.069,
            optional=False,
            minValue=0.0,
            maxValue=1.0,
        )
        qppn.setMetadata({"widget_wrapper": {"decimals": 3}})
        self.addParameter(qppn)
        # raster output
        # DestinationGpkg inherits from QgsProcessingParameterRasterDestination to set default gpkg output format
        self.addParameter(
            QgsProcessingParameterRasterDestinationGpkg(self.OUT_LAYER, self.tr("Raster Knapsack Output layer"))
        )
        # SOLVERS
        value_hints, self.solver_exception_msg = check_solver_availability(SOLVER)
        # solver string combobox (enums
        qpps = QgsProcessingParameterString(
            name="SOLVER",
            description="Solver: recommended options string [and executable STATUS]",
        )
        qpps.setMetadata(
            {
                "widget_wrapper": {
                    "value_hints": value_hints,
                    "setEditable": True,  # not working
                }
            }
        )
        qpps.setFlags(qpps.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(qpps)
        # options_string
        qpps2 = QgsProcessingParameterString(
            name="CUSTOM_OPTIONS_STRING",
            description="Override options_string (type a single space ' ' to not send any options to the solver)",
            defaultValue="",
            optional=True,
        )
        qpps2.setFlags(qpps2.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(qpps2)
        # executable file
        qppf = QgsProcessingParameterFile(
            name=self.IN_EXECUTABLE,
            description=self.tr("Set solver executable file [REQUIRED if STATUS]"),
            behavior=QgsProcessingParameterFile.File,
            # extension="exe" if platform_system() == "Windows" else "",
            optional=True,
            fileFilter="binary (*)",
            defaultValue="/opt/ibm/ILOG/CPLEX_Studio2211/cplex/bin/x86-64_linux/cplex",
        )
        qppf.setFlags(qppf.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(qppf)

    def checkParameterValues(self, parameters, context) -> tuple[bool, str]:
        """log file exists and is not empty"""
        # solver = Path(self.parameterAsString(parameters, self.IN_EXECUTABLE, context))
        # from stat import S_IXGRP, S_IXOTH, S_IXUSR
        # chmod(c2f_bin, st.st_mode | S_IXUSR | S_IXGRP | S_IXOTH)
        return True, ""

    def processAlgorithm(self, parameters, context, feedback):
        # feedback.pushCommandInfo(f"processAlgorithm START")
        # feedback.pushCommandInfo(f"parameters {parameters}")
        # feedback.pushCommandInfo(f"context args: {context.asQgisProcessArguments()}")

        # ?
        # feedback.reportError(f"context.logLevel(): {context.logLevel()}")
        # context.setLogLevel(context.logLevel()+1)

        # report solver unavailability
        feedback.pushInfo(f"Solver unavailability:\n{self.solver_exception_msg}\n")

        # get raster data
        value_layer = self.parameterAsRasterLayer(parameters, self.IN_VALUE, context)
        value_data = get_raster_data(value_layer)
        value_nodata = get_raster_nodata(value_layer, feedback)
        value_map_info = get_raster_info(value_layer)

        weight_layer = self.parameterAsRasterLayer(parameters, self.IN_WEIGHT, context)
        weight_data = get_raster_data(weight_layer)
        weight_nodata = get_raster_nodata(weight_layer, feedback)
        weight_map_info = get_raster_info(weight_layer)

        # raster(s) conditions
        if not value_layer and not weight_layer:
            feedback.reportError("No input layers, need at least one raster!")
            return {self.OUT_LAYER: None, "SOLVER_STATUS": None, "SOLVER_TERMINATION_CONDITION": None}
        elif value_layer and weight_layer:
            # TODO == -> math.isclose
            if not (
                value_map_info["width"] == weight_map_info["width"]
                and value_map_info["height"] == weight_map_info["height"]
                and value_map_info["cellsize_x"] == weight_map_info["cellsize_x"]
                and value_map_info["cellsize_y"] == weight_map_info["cellsize_y"]
            ):
                feedback.reportError("Layers must have the same width, height and cellsizes")
                return {self.OUT_LAYER: None, "SOLVER_STATUS": None, "SOLVER_TERMINATION_CONDITION": None}
            width, height, extent, crs, _, _ = value_map_info.values()
        elif value_layer and not weight_layer:
            width, height, extent, crs, _, _ = value_map_info.values()
            weight_data = np.ones(height * width)
        elif not value_layer and weight_layer:
            width, height, extent, crs, _, _ = weight_map_info.values()
            value_data = np.ones(height * width)

        # instance summary
        N = width * height

        feedback.pushInfo(
            f"width: {width}, height: {height}, N:{N}\n"
            f"extent: {extent}, crs: {crs}\n"
            "\n"
            f"value !=0: {np.any(value_data!=0)}\n"
            f"nodata: {value_nodata}\n"
            f"preview: {value_data}\n"
            f"stats: {stats.describe(value_data[value_data!=value_nodata])}\n"
            "\n"
            f"weight !=1: {np.any(weight_data!=1)}\n"
            f"nodata: {weight_nodata}\n"
            f"preview: {weight_data}\n"
            f"stats: {stats.describe(weight_data[weight_data!=weight_nodata])}\n"
        )
        if isinstance(value_nodata, list):
            feedback.pushError(f"value_nodata: {value_nodata} is list, not implemented!")
        if isinstance(weight_nodata, list):
            feedback.pushError(f"weight_nodata: {weight_nodata} is list, not implemented!")
        # nodata fest
        if value_nodata is None and weight_nodata is None:
            pass
        elif value_nodata is None and weight_nodata is not None:
            self.NODATA = weight_nodata
        elif value_nodata is not None and weight_nodata is None:
            self.NODATA = value_nodata
        elif value_nodata == weight_nodata:
            self.NODATA = value_nodata
        elif value_nodata != weight_nodata:
            feedback.pushWarning(f"Rasters have different nodata values: {value_nodata=}, {weight_nodata=}")
        feedback.pushDebugInfo(f"Using {self.NODATA=}\n")

        no_indexes = np.union1d(np.where(value_data == value_nodata)[0], np.where(weight_data == weight_nodata)[0])
        feedback.pushInfo(f"discarded pixels (no_indexes): {len(no_indexes)/N:.2%}\n")

        response, status, termCondition = do_knapsack(
            self, value_data, weight_data, no_indexes, feedback, parameters, context
        )
        response.resize(height, width)

        undecided, nodata, not_selected, selected = np.histogram(response, bins=[-2, -1, 0, 1, 2])[0]
        feedback.pushInfo(
            "Solution histogram:\n"
            f"{selected=}\n"
            f"{not_selected=}\n"
            f"{nodata=} (invalid value or weight)\n"
            f"{undecided=}\n"
        )
        response[response == -1] = self.NODATA

        output_layer_filename = self.parameterAsOutputLayer(parameters, self.OUT_LAYER, context)
        outFormat = get_output_raster_format(output_layer_filename, feedback)

        array2rasterInt16(
            response,
            "knapsack",
            output_layer_filename,
            extent,
            crs,
            nodata=self.NODATA,
        )
        feedback.setProgress(100)
        feedback.setProgressText("Writing new raster to file ended, progress 100%")

        # if showing
        if context.willLoadLayerOnCompletion(output_layer_filename):
            layer_details = context.layerToLoadOnCompletionDetails(output_layer_filename)
            layer_details.groupName = "DecisionOptimizationGroup"
            layer_details.name = "KnapsackRaster"
            # layer_details.layerSortKey = 2
            processing.run(
                "native:setlayerstyle",
                {
                    "INPUT": output_layer_filename,
                    "STYLE": str(Path(__file__).parent / "knapsack_raster.qml"),
                },
                context=context,
                feedback=feedback,
                is_child_algorithm=True,
            )
            feedback.pushDebugInfo(f"Showing layer {output_layer_filename}")

        write_log(feedback, name=self.name())
        return {
            self.OUT_LAYER: output_layer_filename,
            "SOLVER_STATUS": status,
            "SOLVER_TERMINATION_CONDITION": termCondition,
        }

    def name(self):
        """
        Returns the algorithm name, used for identifying the algorithm. This
        string should be fixed for the algorithm, and must not be localised.
        The name should be unique within each provider. Names should contain
        lowercase alphanumeric characters only and no spaces or other
        formatting characters.
        """
        return "rasterknapsack"

    def displayName(self):
        """
        Returns the translated algorithm name, which should be used for any
        user-visible display of the algorithm name.
        """
        return self.tr("Raster Knapsack")

    def group(self):
        return self.tr("Decision Optimization")

    def groupId(self):
        return "do"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return RasterKnapsackAlgorithm()

    def helpUrl(self):
        return "https://www.github.com/fdobad/qgis-processingplugin-template/issues"

    def shortDescription(self):
        return self.tr(
            """Optimizes the classical knapsack problem using layers as values and/or weights, returns a layer with the selected pixels."""
        )

    def helpString(self):
        return self.shortHelpString()

    def icon(self):
        return QIcon(":/plugins/fireanalyticstoolbox/assets/firebreakmap.svg")

    def shortHelpString(self):
        return self.tr(
            """By selecting a Values layer and/or a Weights layer, and setting the bound on the total capacity, a layer that maximizes the sum of the values of the selected pixels is created.

            A new raster (default .gpkg) will show selected pixels in red and non-selected green (values 1, 0 and no-data=-1).

            The capacity constraint is set up by choosing a ratio (between 0 and 1), that multiplies the sum of all weights (except no-data). Hence 1 selects all pixels that aren't no-data in both layers.

            This raster knapsack problem is NP-hard, so a MIP solver engine is used to find "nearly" the optimal solution (**), because -often- is asymptotically hard to prove the optimal value. So a default gap of 0.5% and a timelimit of 5 minutes cuts off the solver run. The user can experiment with these parameters to trade-off between accuracy, speed and instance size(*). On Windows closing the blank terminal window will abort the run!

            By using Pyomo, several MIP solvers can be used: CBC, GLPK, Gurobi, CPLEX or Ipopt; If they're accessible through the system PATH, else the executable file can be selected by the user. Installation of solvers is up to the user.

            Although windows version is bundled with CBC unsigned binaries, so their users may face a "Windows protected your PC" warning, please avoid pressing the "Don't run" button, follow the "More info" link, scroll then press "Run anyway". Nevertheless windows cbc does not support multithreading, so ignore that warning (or switch to Linux).

            (*): Complexity can be reduced greatly by rescaling and/or rounding values into integers, or even better coarsing the raster resolution (see gdal translate resolution).
            (**): There are specialized knapsack algorithms that solve in polynomial time, but not for every data type combination; hence using a MIP solver is the most flexible approach.

            ----

            USE CASE:

            If you want to determine where to allocate fuel treatments throughout the landscape to protect a specific value that is affected by both the fire and the fuel treatments, use the following:

                - Values: Downstream Protection Value layer calculated with the respective value that you want to protect.

                - Weights: The layer, that contains the value that you want to protect and that is affected also by the fuel treatments (e.g., animal habitat).
            If you want to determine where to allocate fuel treatments through out the landscape to protect and specific value that is affected by both, the fire and the fuel treatments use: 
            """
        )


def do_knapsack(self, value_data, weight_data, no_indexes, feedback, parameters, context):
    """paste into processAlgorithm to be used in any KnapsackAlgorithm
    Returns:
        np.array: response with values 1 selected, 0 not selected, -1 for left out indexes, -2 for solver undecided
        length of response is equal to the length of value_data and weight_data
    """
    N = len(value_data)
    mask = np.ones(N, dtype=bool)
    mask[no_indexes] = False

    ratio = self.parameterAsDouble(parameters, self.IN_RATIO, context)
    weight_sum = weight_data[mask].sum()
    capacity = np.round(weight_sum * ratio)
    feedback.pushInfo(f"capacity bound: {ratio=}, {weight_sum=}, {capacity=}\n")

    m = pyo.ConcreteModel()
    m.N = pyo.RangeSet(0, N - len(no_indexes) - 1)
    m.Cap = pyo.Param(initialize=capacity)
    m.We = pyo.Param(m.N, within=pyo.Reals, initialize=weight_data[mask])
    m.Va = pyo.Param(m.N, within=pyo.Reals, initialize=value_data[mask])
    m.X = pyo.Var(m.N, within=pyo.Binary)
    obj_expr = pyo.sum_product(m.X, m.Va, index=m.N)
    m.obj = pyo.Objective(expr=obj_expr, sense=pyo.maximize)

    def capacity_rule(m):
        return pyo.sum_product(m.X, m.We, index=m.N) <= m.Cap

    m.capacity = pyo.Constraint(rule=capacity_rule)

    executable = self.parameterAsString(parameters, self.IN_EXECUTABLE, context)
    feedback.pushDebugInfo(f"exesolver_string:{executable}")

    solver_string = self.parameterAsString(parameters, "SOLVER", context)
    # feedback.pushDebugInfo(f"solver_string:{solver_string}")

    solver_string = solver_string.replace(" MUST SET EXECUTABLE", "")

    solver, options_string = solver_string.split(": ", 1) if ": " in solver_string else (solver_string, "")
    # feedback.pushDebugInfo(f"solver:{solver}, options_string:{options_string}")

    if len(custom_options := self.parameterAsString(parameters, "CUSTOM_OPTIONS_STRING", context)) > 0:
        if custom_options == " ":
            options_string = None
        else:
            options_string = custom_options
    feedback.pushDebugInfo(f"options_string: {options_string}\n")

    if executable:
        opt = SolverFactory(solver, executable=executable)
        # FIXME if solver is cplex_persistent
        # opt.set_instance(m)
    else:
        opt = SolverFactory(solver)

    feedback.setProgress(20)
    feedback.setProgressText("pyomo model built, solver object created 20%")

    pyomo_std_feedback = FileLikeFeedback(feedback, True)
    pyomo_err_feedback = FileLikeFeedback(feedback, False)
    with redirect_stdout(pyomo_std_feedback), redirect_stderr(pyomo_err_feedback):
        if options_string:
            results = opt.solve(m, tee=True, options_string=options_string)
        else:
            results = opt.solve(m, tee=True)
        # TODO
        # # Stop the algorithm if cancel button has been clicked
        # if feedback.isCanceled():

    status = results.solver.status
    termCondition = results.solver.termination_condition
    feedback.pushConsoleInfo(f"Solver status: {status}, termination condition: {termCondition}")
    if (
        status in [SolverStatus.error, SolverStatus.aborted, SolverStatus.unknown]
        and termCondition != TerminationCondition.intermediateNonInteger
    ):
        feedback.reportError(f"Solver status: {status}, termination condition: {termCondition}")
        return {self.OUTPUT_layer: None, "SOLVER_STATUS": status, "SOLVER_TERMINATION_CONDITION": termCondition}
    if termCondition in [
        TerminationCondition.infeasibleOrUnbounded,
        TerminationCondition.infeasible,
        TerminationCondition.unbounded,
    ]:
        feedback.reportError(f"Optimization problem is {termCondition}. No output is generated.")
        return {self.OUTPUT_layer: None, "SOLVER_STATUS": status, "SOLVER_TERMINATION_CONDITION": termCondition}
    if not termCondition == TerminationCondition.optimal:
        feedback.pushWarning(
            "Output is generated for a non-optimal solution! Try running again with different solver options or"
            " tweak the layers..."
        )
    feedback.setProgress(90)
    feedback.setProgressText("pyomo integer programming finished, progress 80%")
    # pyomo solution to squared numpy array
    response = np.array([pyo.value(m.X[i], exception=False) for i in m.X])
    feedback.pushDebugInfo(f"raw {response=}, {response.shape=}")
    # -2 undecided
    response[response == None] = -2
    response = response.astype(np.int16)
    # -1 left out
    base = -np.ones(N, dtype=np.int16)
    base[mask] = response
    return base, status, termCondition
