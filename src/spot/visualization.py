import html
import re
from typing import Sequence, overload

import colored
import ipywidgets as widgets
import plotly.express as px

from spot.data import (
    ChunkedDataset,
    CountedAcc,
    CtxArgs,
    PythonType,
    SrcDataset,
)
from spot.model import DatasetPredResult, DecodingArgs
from spot.type_check import normalize_type
from spot.utils import *

from io import StringIO
from IPython.display import HTML
from ipywidgets.embed import embed_minimal_html


def display_persist(widget) -> None:
    """
    Persist a widget as HTML content using `embed_minimal_html` and display
    it in the notebook. This helps keeping the content accessable after the
    kernel is restarted.
    """
    if isinstance(widget, widgets.Widget):
        try:
            page = StringIO()
            embed_minimal_html(page, widget)
            display(HTML(page.getvalue()))
        except Exception as e:
            logging.warn(f"Failed to render widget as HTML: {e}")
            display(widget)
    else:
        display(widget)


def visualize_chunks(chunks: list[dict]) -> widgets.VBox:
    def show(i):
        d = chunks[i]
        print("Labels:", decode_tokens(d["labels"]))
        print("============== code =================")
        print(decode_tokens(d["input_ids"]))

    return widgets.interactive(show, i=(0, len(chunks) - 1))


def display_as_widget(x) -> widgets.Output:
    with (out := widgets.Output()):
        display(x)
    return out


def visualize_chunk(
    input_ids: list[int],
    pred_types: list[PythonType],
    label_types: list[PythonType],
):
    def id_replace(id: int) -> str:
        p = pred_types[id]
        t = label_types[id]
        correct = normalize_type(p) == normalize_type(t)
        id_str = f"prediction-{id}"
        if correct:
            return f"<span id='{id_str}' style='color: green;'>{str(p)}</span>"
        else:
            return f"<span id='{id_str}' style='color: red;'>{str(p)} (Gold: {str(t)})</span>"

    code_dec = DefaultTokenizer.decode(input_ids, skip_special_tokens=False)
    code = colorize_code_html(html.escape(code_dec))
    code = code_inline_extra_ids(code, id_replace)
    return widgets.HTML(
        "<pre style='line-height: 1.2; padding: 10px; color: rgb(212,212,212); background-color: rgb(30,30,30);'>"
        + code
        + "</pre>"
    )


def colorize_code_html(code: str, comment_color: str = "orange") -> str:
    "Highlight the special comments in the type checker-augmented python code."
    output = list[str]()
    in_comment = False
    for i in range(len(code)):
        c = code[i]
        prev = code[i - 1] if i > 0 else None
        next = code[i + 1] if i < len(code) - 1 else None
        if not in_comment and c == "/" and next == "*":
            output.append(f"<span style='color: {comment_color}'>")
            in_comment = True
        output.append(c)
        if in_comment and prev == "*" and c == "/":
            output.append("</span>")
            in_comment = False

    return "".join(output)


def code_inline_extra_ids(code: str, id2replace: Callable[[int], str]):
    def replace(m: re.Match[str]):
        mi = re.match(r"&lt;extra_id_(\d+)&gt;", m[0])
        assert mi is not None
        id = int(mi.group(1))
        return id2replace(id)

    return re.sub(r"(&lt;extra_id_\d+&gt;)", replace, code)


def export_preds_on_code(
    dataset: ChunkedDataset,
    preds: list[list[Any]],
    preds_extra: dict[str, list[list[Any]]],
    export_to: Path,
):
    if export_to.exists():
        shutil.rmtree(export_to)
    (export_to / "chunks").mkdir(parents=True)
    for i in tqdm(range(len(dataset.data)), desc="Exporting"):
        page = visualize_chunk(
            dataset.data[i]["input_ids"], preds[i], dataset.chunks_info[i].types
        )
        assert isinstance(page.value, str)
        write_file(export_to / "chunks" / f"chunk{i}.html", page.value)

    chunk_accs = list[CountedAcc]()
    with tqdm(total=len(preds), desc="Computing accuracies") as pbar:
        for info, ps in zip(dataset.chunks_info, preds):
            n_correct = sum(
                normalize_type(p) == normalize_type(t) for p, t in zip(ps, info.types)
            )
            chunk_accs.append(CountedAcc(n_correct, len(ps)))
            pbar.update()

    chunk_sorted = sorted(range(len(chunk_accs)), key=lambda i: chunk_accs[i].acc)
    links = "\n".join(
        f"<li><a href='chunks/chunk{i}.html#prediction-0'>chunk{i} (Acc: {chunk_accs[i]})</a></li>"
        for i in chunk_sorted
    )
    index = f""" Chunks sorted by accuracy (from low to high).
    <ol> {links} </ol>
    """
    write_file(export_to / "index.html", index)
    return None


def visualize_preds_on_code(
    dataset: ChunkedDataset,
    preds: list[list[Any]],
    preds_extra: dict[str, list[list[Any]]],
):
    assert_eq(len(dataset.data), len(preds))

    def show_chunk(i: int):
        assert_eq(int(dataset.data[i]["chunk_id"]), i)
        pred_types = preds[i]
        meta_data = dict[str, list]()
        label_types = dataset.chunks_info[i].types
        meta_data["label_types"] = label_types
        prev_types = dataset.chunks_info[i].prev_types
        if prev_types is not None:
            meta_data["prev_types"] = prev_types
            prev_correct = [
                normalize_type(t) == normalize_type(l)
                for t, l in zip(prev_types, label_types)
            ]
            meta_data["prev_correct"] = prev_correct
        meta_data["predictions"] = pred_types
        for k, v in preds_extra.items():
            meta_data[k] = v[i]

        src_ids = sorted(list(set(dataset.chunks_info[i].src_ids)))
        files = [dataset.files[sid] for sid in src_ids]

        code = visualize_chunk(dataset.data[i]["input_ids"], pred_types, label_types)

        rows = [
            in_scroll_pane(display_as_widget(pd.DataFrame(meta_data)), height="100px"),
            in_scroll_pane(str(files), height=None),
            in_scroll_pane(code),
        ]
        return widgets.VBox(rows)

    slider = widgets.IntSlider(
        0, min=0, max=len(dataset.data) - 1, continuous_update=False
    )
    panel = widgets.VBox([show_chunk(slider.value)])

    def on_slider_change(change):
        panel.children = (show_chunk(change["new"]),)

    slider.observe(on_slider_change, names="value")  # type: ignore

    return widgets.VBox([slider, panel])


def visualize_sequence(
    contents: Sequence[str | widgets.Widget], height: Optional[str] = "500px"
) -> widgets.VBox:
    assert len(contents) > 0

    slider = widgets.IntSlider(min=0, max=len(contents) - 1, value=0)
    slider_label = widgets.Label(value=f"({len(contents)} total)")

    def select(i: int):
        el = contents[i]
        if isinstance(el, str):
            print(el)
        else:
            display(el)

    out = widgets.interactive_output(select, {"i": slider})
    if height is not None:
        out.layout.height = height  # type: ignore
    box_layout = widgets.Layout(overflow="scroll")
    return widgets.VBox(
        [
            widgets.HBox([slider, slider_label]),
            widgets.Box((out,), layout=box_layout),
        ]
    )


def visualize_sequence_tabs(
    contents: Sequence[str | widgets.Widget],
    height: Optional[str] = None,
    titles: Sequence[str] | None = None,
    selected: int | None = None,
) -> widgets.VBox:
    assert len(contents) > 0

    children = list[widgets.Widget]()
    for el in contents:
        if isinstance(el, str):
            el = string_widget(el)
        children.append(el)

    out = widgets.Tab(children=children)
    for i in range(len(children)):
        title = titles[i] if titles is not None else str(i)
        out.set_title(i, title)
    if height is not None:
        out.layout.height = height  # type: ignore
    box_layout = widgets.Layout(overflow="scroll")

    if selected is None:
        selected = len(contents) - 1
    out.selected_index = selected

    return widgets.VBox((out,), layout=box_layout)


def in_scroll_pane(
    content: widgets.Widget | str, height: Optional[str] = "500px"
) -> widgets.Box:
    if isinstance(content, str):
        with (out := widgets.Output()):
            print(content)
        content = out
    box_layout = widgets.Layout(overflow="scroll", height=height)
    return widgets.Box([content], layout=box_layout)


def interactive_sized(
    f,
    kwargs: dict,
    height: Optional[str] = "500px",
) -> widgets.VBox:
    out = widgets.interactive(f, **kwargs)
    panel = out.children[-1]
    return widgets.VBox(
        [
            *out.children[:-1],
            in_scroll_pane(panel, height=height),
        ]
    )


def visualize_dicts(dicts: Sequence[dict], titles: Sequence[str] | None = None):
    def show_dict_with_change(d: dict, prev: Optional[dict]):
        result = dict()
        for k in d:
            v = d[k]
            v0 = prev.get(k, None) if prev is not None else None
            match v, v0:
                case (CountedAcc() as v, CountedAcc() as v0):
                    result[k] = f"{str(v)} [{v.acc - v0.acc:+.2%}]"
                case (CountedAcc(), _):
                    result[k] = f"{str(v)}"
                case (float(), float()) | (int(), int()):
                    result[k] = f"{v:.4g} [{v - v0:.4g}]"
                case (float(), _) | (int(), _):
                    result[k] = f"{v:.4g}"
                case (dict(), dict() | None):
                    result[k] = show_dict_with_change(v, v0)
                case _:
                    result[k] = str(v)
        return result

    def display_acc(round):
        d = dicts[round]
        prev = None if round == 0 else dicts[round - 1]
        return dict_widget(show_dict_with_change(d, prev))

    tabs = [display_acc(i) for i in range(len(dicts))]
    if titles is None:
        titles = [f"R{i}" for i in range(len(dicts))]
    return visualize_sequence_tabs(tabs, titles=titles)


def visualize_conf_matrix(results: dict[str, DatasetPredResult], top_k: int = 15):
    def show_conf(name, top_k):
        pred_r = results[name]
        labels = [
            normalize_type(t).head_name()
            for info in pred_r.chunks.chunks_info
            for t in info.types
        ]
        all_preds = [
            normalize_type(t).head_name() for t in seq_flatten(pred_r.predictions)
        ]
        unique_types = len(set(labels))
        top_k = min(top_k, unique_types)
        m = confusion_matrix_top_k(all_preds, labels, top_k)
        display_conf_matrix(m)

    tabs = []
    for name in results:
        with (out := widgets.Output()):
            show_conf(name, top_k)
        tabs.append(out)

    return visualize_sequence_tabs(tabs, titles=list(results.keys()))


def code_inline_type_masks(code: str, preds: list, label_color: Optional[str] = None):
    i = 0
    if label_color is not None:
        color_mark = colored.fg(label_color)
        reset_mark = colored.attr("reset")

    def replace(m: re.Match[str]):
        nonlocal i
        l = str(preds[i])
        i += 1
        if label_color is not None:
            l = color_mark + l + reset_mark
        return l

    return re.sub(SpecialNames.TypeMask, replace, code)


def string_widget(s: str):
    return widgets.HTML(string_to_html(s))


def string_to_html(s: str) -> str:
    return f"<div style='white-space: pre-wrap; line-height: 1.2; font-family: monospace, monospace;'>{s}</div>"


def dict_widget(d: dict, float_precision: int = 5):
    outputs = list[widgets.Widget]()
    for expand in [False, True]:
        max_level = 1000 if expand else 0
        d_s = pretty_show_dict(
            d, float_precision=float_precision, max_show_level=max_level
        )
        o = widgets.HTML(string_to_html(d_s))
        outputs.append(o)

    tab = widgets.Tab()
    tab.children = outputs
    tab.set_title(0, "Compressed")
    tab.set_title(1, "Expanded")
    return tab


def visualize_counts(
    values: Counter[str] | dict[str, Counter[str]],
    x_name: str,
    top_k: int | Iterable[str] = 15,
    title: str | None = None,
):
    if isinstance(values, Counter):
        values = {"Source": values}
    y_names = list(values.keys())
    counters = list(values.values())
    if isinstance(top_k, int):
        top_keys = [k for k, _ in counters[0].most_common(top_k)]
    else:
        top_keys = top_k
    data = list[dict]()
    for s in y_names:
        c = values[s]
        for k in top_keys:
            data.append({x_name: k, "Count": c.get(k, 0), "Source": s})
    df = pd.DataFrame(data)
    if title is None:
        title = f"{x_name} distribution"
    return px.bar(df, x=x_name, y="Count", color="Source", title=title)


import plotly.express as px

from spot.type_env import MypyFeedback
from spot.utils import groupby, pretty_print_dict


def plot_feedback_distribution(
    feedbacks: Iterable[MypyFeedback],
):
    error_code_counter = Counter[str]()
    for fb in feedbacks:
        error_code_counter[fb.error_code] += 1
    top_feedbacks = dict(error_code_counter.most_common(10))
    df = pd.DataFrame(error_code_counter.most_common(), columns=["error_code", "count"])
    display(px.bar(df, x="error_code", y="count", title="Error code frequencies"))
    return top_feedbacks


def show_feedback_stats(dataset: SrcDataset):
    fb_list: list[list[MypyFeedback]] = dataset.extra_stats["mypy_feedbacks"]
    stats = {}
    for k in ["feedbacks_per_file", "type_check_success_ratio"]:
        stats[k] = dataset.extra_stats[k]
    stats["total_feedbacks"] = sum(len(l) for l in fb_list)
    num_labels = sum(len(s.types) for s in dataset.all_srcs)
    stats["feedbacks_per_label"] = stats["total_feedbacks"] / num_labels
    stats["fraction_files_with_feedbacks"] = float(
        np.mean([1 if len(l) > 0 else 0 for l in fb_list])
    )
    stats["top_feedbacks"] = plot_feedback_distribution(seq_flatten(fb_list))
    pretty_print_dict(stats)
    fdbk_srcs = [(f, src) for src, fs in zip(dataset.all_srcs, fb_list) for f in fs]
    error_groups = groupby(fdbk_srcs, lambda x: x[0].error_code)
    return error_groups


def visualize_feedbacks_in_srcs(
    dataset: SrcDataset,
):
    error_groups = show_feedback_stats(dataset)
    fdbks = list(seq_flatten(list(error_groups.values())))
    n_total = len(fdbks)

    def viz(i):
        fdbk, src = fdbks[i]
        code = code_inline_type_masks(src.origin_code, src.types)
        text = (
            f"feedback: {fdbk}\n" + "=========code=========\n" + add_line_numbers(code)
        )
        display(string_widget(text))

    return interactive_sized(viz, {"i": (0, n_total - 1)})
