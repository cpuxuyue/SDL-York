import base64
import io
import json
import os
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from rdkit import Chem
from rdkit.Chem import Draw
from rdkit.Chem.Draw import rdMolDraw2D

base_url = "http://192.168.1.11:8700"
st.set_page_config(
    page_icon="🦾",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_data
def get_lipid_id2smiles():
    lipid_library_file = "../../../model/data_process/220k_library_with_meta.csv"
    assert os.path.exists(lipid_library_file), "The lipid library file does not exist."
    lipid_library = pd.read_csv(lipid_library_file)
    # select columns
    lipid_library = lipid_library[
        [
            "component_str",
            "combined_mol_SMILES",
            "A_name",
            "A_smiles",
            "B_name",
            "B_smiles",
            "C_name",
            "C_smiles",
            "D_name",
            "D_smiles",
        ]
    ]
    lipid_id2smiles = lipid_library.set_index("component_str").to_dict(orient="index")
    return lipid_id2smiles


lipid_id2smiles = get_lipid_id2smiles()


def get_entries():
    # Placeholder for the method that fetches available entry IDs
    # Replace with the actual implementation
    request_url = base_url + "/entries"
    return requests.get(request_url).json()


def get_entry_readings(entry_id):
    request_url = base_url + f"/entry/{entry_id}/readings"
    response = requests.get(request_url)
    if response.status_code == 200:
        return response.json()
    else:
        st.error("Failed to fetch data. Please check the entry ID.")


def get_entry_gain(entry_id):
    """Get the gain values of the the experiment readings."""
    request_url = base_url + f"/entry/{entry_id}/gain"
    response = requests.get(request_url)
    if response.status_code == 200:
        return response.json()
    else:
        raise ValueError("Failed to fetch data. Please check the entry ID.")


def load_regressor_from_json(file_path=None):
    """Loads the linear regression model from JSON file and returns a simple callable function."""
    if file_path is None:
        cur_file_dir = Path(__file__).parent
        file_path = (
            cur_file_dir
            / "../../../../model/evaluation/notebooks/empty_control_calib/linear_regressor.json"
        )
    assert os.path.exists(file_path), f"The regressor file does not exist: {file_path}"

    with open(file_path, "r") as json_file:
        regressor_data = json.load(json_file)

    # Return a function that calculates y = mx + b
    def regressor_function(gain_value):
        return regressor_data["slope"] * gain_value + regressor_data["intercept"]

    return regressor_function


def log_and_norm_reading(readings, gain_values=None) -> Dict[str, Dict]:
    """loop and call the _log_and_norm_reading function"""
    if gain_values is None:
        return {key: _log_and_norm_reading(value) for key, value in readings.items()}
    else:
        return {
            key: _log_and_norm_reading(value, gain_values[key])
            for key, value in readings.items()
        }


def _log_and_norm_reading(reading, gain_value=None, clip_negative=True):
    """
    Normalize the readings by calling the log and norm function. Essentially, it logs the readings and normalizes them by the control well readings. The first two wells that have none components are the control wells. The others of none components are the benchmark lipids of MC3. The rest are the experimental wells.

    Args:
        readings (dict): A dictionary containing the readings for each well.
        gain_value (optional, float): The gain value of the experiment. If provided, the empty control well readings will be inferred from the gain value.
        example:
        {
            "A1": {"reading": 0.0, "components": {...}},
            "A2": {"reading": 0.0, "components": {...}},
            ...
        }
        clip_negative (bool): Whether to clip the negative values to zero.

    Returns:
        dict: A dictionary containing the normalized readings for each well.
        example:
        {
            "A1": {"reading": 0.0, "components": {...}, "type": "control"},
            "A2": {"reading": 0.0, "components": {...}, "type": "mc3"},
            ...
        }
    """
    # log transform the data
    log_reading = {}
    for key, value in reading.items():
        log_reading[key] = {
            "reading": np.log2(value["reading"]),
            "components": value["components"],
        }

    # find all wells that have none components
    empty_and_positive_control_wells = []
    for key, value in reading.items():
        if value["components"]["amines"] is None:
            empty_and_positive_control_wells.append(key)
    empty_wells = empty_and_positive_control_wells[:2]
    positive_control_wells = empty_and_positive_control_wells[2:]
    assert empty_wells[0] == "A1"
    assert empty_wells[1] == "B1"

    # assign the type of the well
    for key in log_reading.keys():
        if key in empty_wells:
            log_reading[key]["type"] = "control"
        elif key in positive_control_wells:
            log_reading[key]["type"] = "mc3"
        else:
            log_reading[key]["type"] = "experimental"

    # qc of control wells
    min_control_reading = min([log_reading[key]["reading"] for key in empty_wells])
    if min_control_reading > np.log2(300):
        st.write(
            f"The control well readings are too high: {np.exp2(min_control_reading):.2f}"
            f" Probably due to light leakage. Autoset control reading to 300."
        )
        min_control_reading = np.log2(300)
    for key in empty_wells:
        if log_reading[key]["reading"] > np.log2(300):
            log_reading[key]["reading"] = min_control_reading

    # normalize the data
    if gain_value is not None:
        # calculate the control well readings from the gain value
        ctrl = load_regressor_from_json()(gain_value)
        print(f"Inferred log control reading from gain {gain_value}: {ctrl:.2f}")
    else:
        ctrl = np.mean([log_reading[key]["reading"] for key in empty_wells])
    for key in log_reading.keys():
        log_reading[key]["reading"] = log_reading[key]["reading"] - ctrl
        if clip_negative:
            log_reading[key]["reading"] = max(log_reading[key]["reading"], 0)

    return log_reading


def qc_entry_readings(nomalized_readings, threshold=3):
    """
    Quality control the readings for each well in the 96-well plate. Each entry can have readings of maximum four replicates. The first two are two repeated readings of one well, and the other two are two repeated readings of another well. Sometimes there are only two replicates, then the third and fourth readings are missing.

    1. check whther the readings are trustworthy: if the difference between the two readings is less than `threshold`, then the readings are trustworthy.
    2. If the readings are trustworthy, then calculate the average of the two readings as the final reading for that well.
    3. Across wells for the same lipid structure, using the larger value as the final value.

    Args:
        readings (dict): A dictionary containing the readings for each well.
        example:
        {
            "0": {
                "A1": {"reading": 0.0, "components": {...}, "type": "control"},
                "A2": {"reading": 0.0, "components": {...}, "type": "mc3"},
                ...
            },
            "1": {
                "A1": {"reading": 0.0, "components": {...}, "type": "control"},
                "A2": {"reading": 0.0, "components": {...}, "type": "mc3"},
                ...
            },
            ...
        }
        threshold (float): The threshold to determine whether the readings are trustworthy. Note this is in the log scale.

    Returns:
        dict: A dictionary containing the normalized readings for each well.
        example:
        {
            "A1": {"reading": 0.0, "components": {...}, "type": "control"},
            "A2": {"reading": 0.0, "components": {...}, "type": "mc3"},
            ...
        }
    """
    qc_data = {}
    for key, readings in nomalized_readings.items():
        for well, reading in readings.items():
            if well not in qc_data:
                qc_data[well] = {
                    "reading": [],
                    "components": reading["components"],
                    "type": reading["type"],
                }
            qc_data[well]["reading"].append(reading["reading"])

    def _process_data(data0, data1, threshold, message_prefix=""):
        if abs(data0 - data1) < threshold:
            return np.mean([data0, data1])
        elif max(data0, data1) > 7:
            return max(data0, data1)
        else:
            st.write(
                f"{message_prefix} The difference between the two readings ({data0:.2f},{data1:.2f}) is larger than {threshold}."
            )
            return np.mean([data0, data1])

    for well, data in qc_data.items():
        if len(data["reading"]) == 1:
            qc_data[well]["reading"] = data["reading"][0]
        elif len(data["reading"]) == 2:
            data0, data1 = data["reading"]
            qc_data[well]["reading"] = _process_data(
                data0, data1, threshold, f"Replicates of {well}:"
            )
        elif len(data["reading"]) == 4:
            data0, data1, data2, data3 = data["reading"]
            reading1 = _process_data(
                data0, data1, threshold, f"Replicates #0,1 of well {well}:"
            )
            reading2 = _process_data(
                data2, data3, threshold, f"Replicates #2,3 of well {well}:"
            )

            qc_data[well]["reading"] = max(reading1, reading2)
        else:
            qc_data[well]["reading"] = np.nan

    return qc_data


@st.cache_data
def data2df(integrated_data):
    # make the integrated data to dataframe, including the experimental type readings
    # each row contains the recordings for a lipid structure of AxBxCxDx
    # each row should be |name|max|mean|std|reading1|reading2|reading3|readingN|
    df_data = {}
    for entry_id, data in integrated_data.items():
        for well, reading in data.items():
            if reading["type"] == "experimental":
                lipid_structure = "".join(
                    [
                        reading["components"][key]
                        for key in sorted(reading["components"].keys())
                    ]
                )
                if lipid_structure not in df_data:
                    df_data[lipid_structure] = {}
                    df_data[lipid_structure]["amine"] = reading["components"]["amines"]
                    df_data[lipid_structure]["isocyanide"] = reading["components"][
                        "isocyanide"
                    ]
                    df_data[lipid_structure]["aldehyde"] = reading["components"][
                        "lipid_aldehyde"
                    ]
                    df_data[lipid_structure]["carboxylic_acid"] = reading["components"][
                        "lipid_carboxylic_acid"
                    ]
                df_data[lipid_structure][f"reading.{entry_id}"] = reading["reading"]
    # make the dataframe
    df = pd.DataFrame(df_data).T
    # add the mean, std, and max columns
    df.insert(4, "max", df.filter(like="reading").max(axis=1, skipna=True))
    df.insert(5, "mean", df.filter(like="reading").mean(axis=1, skipna=True))
    df.insert(6, "std", df.filter(like="reading").std(axis=1, skipna=True))

    # add the similes column by searching the lipid_id2smiles
    smiles = df.index.map(
        lambda x: (
            lipid_id2smiles[x]["combined_mol_SMILES"] if x in lipid_id2smiles else None
        )
    )
    df.insert(0, "smiles", smiles)

    mols = df["smiles"].map(Chem.MolFromSmiles)
    imgs = mols.map(Draw.MolToImage)  # type of img is PIL.PngImagePlugin.PngImageFile
    base64_imgs = []
    for img in imgs:
        img_byte_array = io.BytesIO()
        img.save(img_byte_array, format="PNG")
        img_byte_array = img_byte_array.getvalue()
        base64_imgs.append(
            "data:image/png;base64," + base64.b64encode(img_byte_array).decode("utf-8")
        )
    imgs = base64_imgs

    df.insert(1, "mol_img", imgs)

    # sort the dataframe by the max value
    df = df.sort_values(by="max", ascending=False)
    return df


@st.cache_data
def create_box_plot(df: pd.DataFrame, column: str, color: str, title: str, sort=True):
    if sort:
        # copy the dataframe
        df = df.copy()

        # Calculate the median of the "max" values for each category
        medians = df.groupby(column)["max"].median().sort_values(ascending=False)

        # Sort the DataFrame based on these median values
        df[column] = df[column].astype("category")
        df[column] = df[column].cat.set_categories(medians.index)
        df.sort_values(by=column, key=lambda col: col.cat.codes, inplace=True)

    # Create the box plot
    return go.Figure(
        data=[
            go.Box(
                y=df["max"],
                x=df[column],
                boxpoints="all",
                jitter=0.3,
                pointpos=-0.8,
                line_color="black",
                fillcolor=color,
                opacity=0.6,
                name=title,
                width=0.6,
                marker=dict(
                    # color="rgba(0,0,0,0)",  # color of the boxpoints to transparent
                    size=3,  # size of the boxpoints
                    line=dict(
                        color="black",  # boxpoint outline color
                        width=1,  # boxpoint outline width
                    ),
                ),
            )
        ],
        layout=go.Layout(
            title=f"Box plot of the max readings per {title.lower()}",
            xaxis_title=title,
            yaxis_title="Max Reading",
        ),
    )


st.title("96-well Plate Readings Heatmap")

# Get available entry IDs
entries = get_entries()

# TODO: move the analysis to separate script and use the DAO instead of info_api
# normalize and integrate all readings
st.subheader("Processing all readings:")
with st.container(height=300):
    integrated_data = {}
    for entry in entries:
        entry_id = entry["id"]
        st.write(f"Processing entry {entry_id}...")

        data = get_entry_readings(entry_id)
        gain_values = get_entry_gain(entry_id)
        normalized_data = log_and_norm_reading(data, gain_values)
        qc_data = qc_entry_readings(normalized_data)

        integrated_data[entry_id] = qc_data
# st.write(integrated_data)
st.divider()
st.subheader("Integrated readings:")
df = data2df(integrated_data)

st.dataframe(
    df,
    column_config={
        "mol_img": st.column_config.ImageColumn(
            "lipid structure", help="Preview Image of the lipid molecules"
        )
    },
    height=450,
)
st.write(f"Number of lipid structures: {len(df)}")

col1, col2 = st.columns(2)
# show the distribution plot of the max and mean readings
col1.plotly_chart(
    go.Figure(
        data=[
            go.Histogram(
                x=df["max"],
                histnorm="probability",
                marker_color="lightsalmon",
                opacity=0.75,
                name="Max Reading",
            ),
            go.Histogram(
                x=df["mean"],
                histnorm="probability",
                marker_color="lightblue",
                opacity=0.75,
                name="Mean Reading",
            ),
        ],
        layout=go.Layout(
            title="Distribution of the max and mean readings",
            xaxis_title="Reading",
            yaxis_title="Probability",
        ),
    )
)
# show the distribution plot of the std readings
col2.plotly_chart(
    go.Figure(
        data=[
            go.Histogram(
                x=df["std"],
                histnorm="probability",
                marker_color="lightgreen",
                opacity=0.75,
                name="Std Reading",
            )
        ],
        layout=go.Layout(
            title="Distribution of the standard deviation readings",
            xaxis_title="Reading",
            yaxis_title="Probability",
            showlegend=True,  # Add this line to show the legend
        ),
    )
)
# show scatter plot of the max vs std readings
col1.plotly_chart(
    go.Figure(
        data=[
            go.Scatter(
                x=df["max"],
                y=df["std"],
                mode="markers",
                text=df.index,
                hovertemplate="Lipid ID: %{text}<br>Max Reading: %{x}<br>Std Reading: %{y}",
                marker=dict(
                    size=10,
                    color="lightsalmon",
                    opacity=0.5,
                    line=dict(width=1, color="DarkSlateGrey"),
                ),
            )
        ],
        layout=go.Layout(
            title="Scatter plot of the max vs std readings",
            xaxis_title="Max Reading",
            yaxis_title="Std Reading",
        ),
    )
)
# show scatter plot of the mean vs std readings
col2.plotly_chart(
    go.Figure(
        data=[
            go.Scatter(
                x=df["mean"],
                y=df["std"],
                mode="markers",
                text=df.index,
                hovertemplate="Lipid ID: %{text}<br>Mean Reading: %{x}<br>Std Reading: %{y}",
                marker=dict(
                    size=10,
                    color="lightblue",
                    opacity=0.5,
                    line=dict(width=1, color="DarkSlateGrey"),
                ),
            )
        ],
        layout=go.Layout(
            title="Scatter plot of the mean vs std readings",
            xaxis_title="Mean Reading",
            yaxis_title="Std Reading",
        ),
    )
)

# Box plots
col1.plotly_chart(create_box_plot(df, "amine", "lightblue", "Amine"))
col2.plotly_chart(
    create_box_plot(df, "isocyanide", "lightgoldenrodyellow", "Isocyanide")
)
col1.plotly_chart(create_box_plot(df, "aldehyde", "lightgreen", "Aldehyde"))
col2.plotly_chart(
    create_box_plot(df, "carboxylic_acid", "lightcoral", "Carboxylic Acid")
)

# Parallel set plot, each axis is a component, and the value is the reading max
# using go.Parcoords
plot_data = df[["amine", "isocyanide", "aldehyde", "carboxylic_acid", "max"]]
plot_data["amine_code"] = plot_data["amine"].astype("category").cat.codes
plot_data["isocyanide_code"] = plot_data["isocyanide"].astype("category").cat.codes
plot_data["aldehyde_code"] = plot_data["aldehyde"].astype("category").cat.codes
plot_data["carboxylic_acid_code"] = (
    plot_data["carboxylic_acid"].astype("category").cat.codes
)

st.plotly_chart(
    go.Figure(
        data=[
            go.Parcoords(
                tickfont=dict(size=12),
                line=dict(
                    color=plot_data["max"],
                    colorscale="Electric",
                    showscale=True,
                    cmin=plot_data["max"].min(),
                    cmax=plot_data["max"].max() + 3,
                ),
                dimensions=[
                    dict(
                        label="Amine",
                        values=plot_data["amine_code"],
                        tickvals=plot_data["amine_code"].unique(),
                        ticktext=plot_data["amine"].unique(),
                    ),
                    dict(
                        label="Isocyanide",
                        values=plot_data["isocyanide_code"],
                        tickvals=plot_data["isocyanide_code"].unique(),
                        ticktext=plot_data["isocyanide"].unique(),
                    ),
                    dict(
                        label="Aldehyde",
                        values=plot_data["aldehyde_code"],
                        tickvals=plot_data["aldehyde_code"].unique(),
                        ticktext=plot_data["aldehyde"].unique(),
                    ),
                    dict(
                        label="Carboxylic Acid",
                        values=plot_data["carboxylic_acid_code"],
                        tickvals=plot_data["carboxylic_acid_code"].unique(),
                        ticktext=plot_data["carboxylic_acid"].unique(),
                    ),
                    dict(
                        label="Max Reading",
                        values=plot_data["max"],
                    ),
                ],
            )
        ],
        layout=go.Layout(
            title="Parallel Coordinates Plot of the max readings",
            xaxis_title="Component",
            yaxis_title="Max Reading",
            font=dict(size=15),
            height=450,
        ),
    ),
    use_container_width=True,
)

st.divider()
st.subheader("Reading of selected entry:")
# with st.expander("Select an entry to visualize"):
entry = st.selectbox("Select an entry ID:", entries, index=None)

if entry:
    entry_id = entry["id"]
    entry_date = entry["last_updated"]
    st.markdown(f"## {entry_date}")

    data = get_entry_readings(entry_id)
    gain_values = get_entry_gain(entry_id)
    normalized_data = log_and_norm_reading(data, gain_values)

    # Extracting readings and hover text
    for key in data.keys():
        wells = sorted(data[key].keys())
        rows = sorted(list(set(well[0] for well in wells)), reverse=True)
        columns = sorted(list(set(int(well[1:]) for well in wells)))

        heatmap_data = np.zeros((len(rows), len(columns)))
        hovertext = np.empty((len(rows), len(columns)), dtype=object)

        for well in wells:
            row_idx = rows.index(well[0])
            col_idx = columns.index(int(well[1:]))
            heatmap_data[row_idx, col_idx] = normalized_data[key][well]["reading"]

            components = data["0"][well]["components"]
            hovertext[row_idx, col_idx] = (
                f"Well: {well}<br>log(reading): {heatmap_data[row_idx, col_idx]:.2f}"
                f"<br>Reading: {data[key][well]['reading']}"
                + (
                    f"<br>Amines: {components['amines']}<br>Isocyanide: {components['isocyanide']}"
                    f"<br>Aldehyde: {components['lipid_aldehyde']}"
                    f"<br>Carboxylic Acid: {components['lipid_carboxylic_acid']}"
                    if normalized_data[key][well]["type"] == "experimental"
                    else f"<br>Type: {normalized_data[key][well]['type']}"
                )
            )

        # blank
        # log transform the data
        fig = go.Figure(
            data=go.Heatmap(
                z=heatmap_data,
                x=[str(col) for col in columns],
                y=rows,
                text=hovertext,
                hoverinfo="text",
                colorscale="Viridis",
            )
        )

        fig.update_layout(
            title=f"96-well Plate Readings Heatmap; rep {key}",
            xaxis_title="Column",
            yaxis_title="Row",
            width=800,
        )

        event = st.plotly_chart(fig, key=f"heatmap_{entry_id}_{key}", on_select="rerun")
        # event.selection

        # on click event, show the details of the lipid structure

# fig = go.Figure(
#     data=go.Heatmap(
#         z=heatmap_data,
#         x=[str(col) for col in columns],
#         y=rows,
#         text=hovertext,
#         hoverinfo="text",
#         colorscale="Viridis",
#     ),
#     layout=go.Layout(
#         title=f"96-well Plate Readings Heatmap; rep {key}",
#         xaxis_title="Column",
#         yaxis_title="Row",
#         width=800,  # Set the width of the figure
#     ),
# )
# event = st.plotly_chart(
#     fig,
#     # key=f"heatmap_{entry_id}_{key}",
#     on_select="rerun",
#     selection_mode="points",
# )
# event.selection

# import streamlit as st
# import plotly.express as px

# df = px.data.iris()  # iris is a pandas DataFrame
# fig = px.scatter(df, x="sepal_width", y="sepal_length")

# event = st.plotly_chart(fig, key="iris", on_select="rerun")

# event
