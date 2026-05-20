"""layer_classifier.py — classify DXF layer names into UI groups.

Strategy: LLM-first. Each unique layer name + a sample of the texts that
appear on it are sent to the LLM, which picks one of the fixed UI
categories. Results are cached to a JSON file keyed on the layer name
so the LLM is called once per (project, layer) pair and reused on later
parses.

The fixed category list keeps the UI grouping consistent across
projects — the LLM does the *mapping*, but the *bucket names* are
ours.

A small rule table is retained only as an emergency fallback when the
LLM is unreachable (no API key / network failure / SDK missing) so the
endpoint never returns a 500.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Categories — must stay in sync with the frontend GROUP_ORDER list.
# ---------------------------------------------------------------------------

# UI に表示する「審査・地図化に必要」なカテゴリ。
# それ以外は「不要」となり、UI からは既定で非表示。
#
# 14項目チェック仕様から逆算した 23 個。同期先:
#   - frontend/src/components/DataExplorer.tsx の GROUP_ORDER
#   - parser.py 側の category → FloorData フィールド振り分け（後続コミット）
USEFUL_CATEGORIES: list[str] = [
    # 躯体・建築
    "通り芯",
    "躯体壁",                  # v3: RC/PCa/S — 構造壁 (旧「内壁」「外壁」の RC 系を統合)
    "乾式壁",                  # v3: ALC/LGS/CB/木軸/パネル — 非構造壁
    "耐火壁・防火区画",        # v2 維持: 仕様 #6 「複合耐火壁」対応
    "柱・仕上線",
    "梁",
    "スラブ外形",
    "スラブラベル",            # v2 分割: 旧「スラブ情報」（F308 / S番号・厚み）
    "スラブFL",                # v2 分割: 旧「スラブ情報」（F155 / 面レベル FL+40 等）
    "段差線",
    "床ヌスミ",
    # 記号・テキスト
    "FL表記",
    "寸法線",
    "P-N番号",
    "部屋名",
    "水勾配",
    "機器コード_衛生",          # v2 分割: 旧「機器コード」 [衛生]系統別
    "機器コード_空調",          # v2 分割: 旧「機器コード」 [空調]通常/その他
    "機器コード_電気",          # v2 分割: 旧「機器コード」 [電気]通常/盤/配置基準
    # スリーブ本体
    "スリーブ_衛生",
    "スリーブ_空調",
    "スリーブ_電気",
    "スリーブ_その他",
]

# 全カテゴリ（LLM が選べる候補）。useful + 不要。
CATEGORIES: list[str] = USEFUL_CATEGORIES + ["不要"]

# ---------------------------------------------------------------------------
# Rule table (priority order — first match wins).
#
# Rules are evaluated against the LAYER NAME only. Sample texts are passed
# to the LLM fallback for ambiguous cases.
# ---------------------------------------------------------------------------

_RULES: list[tuple[re.Pattern, str]] = [
    # ---- スリーブ系 (discipline-aware) — must be matched BEFORE the
    # generic discipline catch-all rules below.
    (re.compile(r"\[衛生\].*スリーブ"), "スリーブ_衛生"),
    (re.compile(r"\[空調\].*スリーブ"), "スリーブ_空調"),
    (re.compile(r"\[電気\].*スリーブ"), "スリーブ_電気"),
    (re.compile(r"スリーブ"),           "スリーブ_その他"),
    (re.compile(r"A858"),              "スリーブ_その他"),

    # ---- P-N 番号 — 衛生通常レイヤーは P-N-x が大量に並ぶ専用層 ----
    (re.compile(r"\[衛生\].*通常"), "P-N番号"),

    # ---- 通り芯 (壁芯ルールより先に判定) ----
    # 「通り芯」を「壁芯」より先に判定しないと、`[電気]通り芯（壁芯）..` の
    # ようにカッコ内に '壁芯' を含む通り芯レイヤーが誤って '不要' になる。
    (re.compile(r"通心|通芯|通り心|通り芯"), "通り芯"),
    (re.compile(r"C13[12]|C141"),            "通り芯"),

    # ---- 壁芯 (C151) — 通り芯判定の後で。
    # 名前に '壁心' / '壁芯' が入るレイヤーは壁の中心線で、
    # 通り芯（grid axis）ではない。
    # v3: 中心参照線は壁本体ではなくスリーブチェックに直接寄与しない → '不要'
    # (v2 では '内壁' に倒していたが、壁本体は別レイヤーから拾える)
    (re.compile(r"C151|壁心|壁芯"), "不要"),

    # ---- 寸法線 — 通り芯 / [衛生]/[電気] catch-all より先に判定。
    # "通心寸法" / "[衛生]文字・寸法" 等が誤分類されるのを防ぐ。
    (re.compile(r"寸法|配管寸|文字・寸法|C16[1234]"), "寸法線"),

    # ---- 機器コード (discipline-aware) — スリーブ・通常・寸法を
    # 上で取り切った後の "[衛生] / [電気] / [空調] のその他系統別" に
    # マッチさせる。配管系・電気系統別レイヤーが該当。
    # v2: 規律別に分離。
    (re.compile(r"\[衛生\]"), "機器コード_衛生"),

    # ---- 壁系 (v3: 材料軸ベース) ----
    # 耐火壁・防火区画は他より優先 (複合耐火壁は法規上の区画線扱い)。
    (re.compile(r"耐火壁|防火区画|防火壁|区画壁|複合耐火|A561|耐火被覆"), "耐火壁・防火区画"),
    # エレベーター鉄骨支柱・F204 鉄骨間柱 を壁ルールより先に判定。
    # `エレベーター_間柱` も `F204_鉄骨間柱` も「間柱」を含むが、
    # 構造系のスチール支柱なので「柱・仕上線」が正解。
    # (A444_下地鉄骨、間柱 = 軽量鉄骨下地 = 乾式壁、とは別)
    (re.compile(r"エレベーター|ＥＶ|EV"),                "柱・仕上線"),
    (re.compile(r"F204|鉄骨間柱"),                       "柱・仕上線"),
    # 躯体壁: RC / 既存躯体外壁 / [建築]壁 等の構造壁。内/外はジオメトリで判定。
    (re.compile(r"F10[56]_RC壁|A421_壁|RC壁|★既存躯体外壁|]外壁"), "躯体壁"),
    (re.compile(r"]壁(?!心|芯|割付|ラベル|開口)"),                "躯体壁"),
    # 乾式壁: ALC / PCa / LGS / 木軸 / CB / パネル / 下地鉄骨
    (re.compile(r"A422|ＡＬＣ|ALC"),                              "乾式壁"),
    (re.compile(r"A423|PCa"),                                     "乾式壁"),
    (re.compile(r"A424|パネル"),                                  "乾式壁"),
    (re.compile(r"A441|LGS|ＬＧＳ"),                              "乾式壁"),
    (re.compile(r"A442|木軸"),                                    "乾式壁"),
    (re.compile(r"A443|ＣＢ|_CB"),                                "乾式壁"),
    (re.compile(r"A444|下地鉄骨|間柱"),                            "乾式壁"),
    # 仕上線 — スリーブチェックには寄与しない (装飾)
    (re.compile(r"A521_壁|壁仕上|壁：仕上|壁:仕上"),               "不要"),

    # ---- 柱 ----
    (re.compile(r"F1[0-2]\d?_RC柱|F101|F102|A411|A412"),  "柱・仕上線"),
    (re.compile(r"F201_Ｓ柱|F201_S柱|A511|A512"),         "柱・仕上線"),
    (re.compile(r"F204|鉄骨間柱|ブレース|F203"),          "柱・仕上線"),

    # ---- 梁 ----
    (re.compile(r"F10[34]_RC梁|RC梁|F202|Ｓ梁|S梁|A431|A432|付帯梁"), "梁"),
    (re.compile(r"F305_RC梁|F305|梁ラベル|梁_ラベル"),                "梁"),

    # ---- スラブ系 ----
    (re.compile(r"F107_RC床|F121_スラブ"),         "スラブ外形"),
    (re.compile(r"F108_2|立上り|RC立上"),           "スラブ外形"),
    (re.compile(r"F108_4|RC開口"),                  "スラブ外形"),
    (re.compile(r"F108_RC見え掛り"),                 "スラブ外形"),
    (re.compile(r"F308|スラブラベル"),               "スラブラベル"),
    (re.compile(r"F155|スラブレベル"),               "スラブFL"),

    # ---- 床ヌスミ / 段差 ----
    (re.compile(r"F108_5|床ヌスミ"),                       "床ヌスミ"),
    (re.compile(r"F108_3|スラブ段差|段差線"),               "段差線"),
    (re.compile(r"段差記号|A244"),                          "段差線"),

    # ---- FL関連 ----
    (re.compile(r"A221_記入文字|A223_レベル"),             "FL表記"),
    (re.compile(r"C132_FL"),                               "FL表記"),

    # ---- 部屋名 / 室名 ----
    (re.compile(r"A211|A212|室名"),                        "部屋名"),

    # ---- 水勾配 ----
    (re.compile(r"水勾配"),                                 "水勾配"),

    # ---- 注釈系 → '注釈・記号' という UI カテゴリは廃止された。
    # 雲マーク / 方位記号 / 建具記号 / 断面記号 はスリーブチェックには寄与
    # しないので '不要' に振る。 (旧バージョンでは USEFUL_CATEGORIES に
    # '注釈・記号' があったが削除済み — ここを残してると LLM 失敗時の
    # rule_fallback で存在しないカテゴリが返ってフロントが壊れる)
    (re.compile(r"注意点"),                                 "不要"),
    (re.compile(r"A245|雲マーク"),                          "不要"),
    (re.compile(r"A241|方位"),                              "不要"),
    (re.compile(r"A242|A243|建具記号|A247_断面記号"),       "不要"),

    # ---- 図面枠 / 凡例 / 表題欄系 → 不要 ----
    (re.compile(r"C111|C112|C113|C114|図枠"),               "不要"),
    (re.compile(r"C121|C122|図面名称|図面属性"),            "不要"),
    (re.compile(r"C200|凡例"),                              "不要"),
    (re.compile(r"ビューポート|VIEWPORT"),                  "不要"),
    (re.compile(r"A711|境界線|A712|境界表示"),               "不要"),

    # ---- 鉄骨・補強系（構造扱い） ----
    # デッキ受け（鉄骨工事の加工指示）は不要 — 下の '不要' ブロックで先に拾われる
    # ように、その前にこの汎用ルールがあると吸われるので注意（順序依存）。
    (re.compile(r"デッキ受け"),                              "不要"),
    # メーカー打込金物は鉄骨側付帯品 (打込金物_三和タジマ / 鉄骨対応_三和タジマ /
    # 打込金物_不二サッシ 等) — 表記が「打込金物」か「鉄骨対応」かに関わらず
    # 中身は同等なので '鉄骨' ルールより先に '不要' に倒す。
    (re.compile(r"三和タジマ|不二サッシ|ISE|ガラス手摺"),    "不要"),
    (re.compile(r"鉄骨|ジョイント|剛接合|ブレース"),        "柱・仕上線"),
    (re.compile(r"フランジ補強"),                            "柱・仕上線"),
    (re.compile(r"間柱_ファスナー|エレベーター_ファスナー"), "柱・仕上線"),
    (re.compile(r"F301|柱構造体|HOJO_柱"),                  "柱・仕上線"),

    # ---- 構造ラベル ----
    (re.compile(r"F306|壁ラベル"),                          "躯体壁"),

    # ---- 床細部 / スラブ周辺 ----
    (re.compile(r"F108_7|根巻|根巻きコン"),                  "スラブ外形"),
    (re.compile(r"F112|基礎"),                              "スラブ外形"),
    (re.compile(r"F401|ルーフドレン"),                       "スラブ外形"),
    (re.compile(r"A311|吹抜"),                              "スラブ外形"),
    (re.compile(r"床ピット|A341"),                           "床ヌスミ"),

    # ---- ハッチング / マーク / 装飾系 → 不要 ----
    (re.compile(r"F153|F154|F410|ハッチング"),               "不要"),
    (re.compile(r"A321|A346|見え掛り|見上げ"),               "不要"),
    (re.compile(r"F151|面取り|F152|誘発目地|F150|打継"),    "不要"),
    (re.compile(r"打込金物|打込BOX|サッシアンカー|F145"),    "不要"),
    (re.compile(r"工区割り|後打|山留|仮設"),                 "不要"),
    (re.compile(r"コン止め|F148|補強筋|F407|F406|F408"),     "不要"),
    (re.compile(r"鉄筋|鉄筋_|主筋|F160|Ｆ160"),               "不要"),
    (re.compile(r"ANOTHER_WORKS|別途|オイルタンク"),         "不要"),
    (re.compile(r"A551|階段|A552|スロープ|A553"),           "不要"),
    (re.compile(r"日付文字|HASEN|HATCH|HOJO\b|TEXT|JISSEN|SAISEN|SUNPOU|ITTEN"), "不要"),
    (re.compile(r"ポスト|文字\b|ZZ_HIDE"),                   "不要"),
    (re.compile(r"DW仮設|RW|^[\[空調建築電気衛生\]]*仮設"), "不要"),
    (re.compile(r"A245|雲マーク|注意点|A244|段差記号"),      "不要"),
    (re.compile(r"A241|方位|A242|A243|建具記号|A247_断面記号"), "不要"),

    # ---- 電気 / 空調の機器・系統別レイヤー (v2: 規律別) ----
    (re.compile(r"\[電気\]"),                                 "機器コード_電気"),
    (re.compile(r"\[空調\]"),                                 "機器コード_空調"),

    # ---- レイヤー "0" / Defpoints → 不要 ----
    (re.compile(r"^0$|^Defpoints$|defpoint"),                "不要"),
    (re.compile(r"^\[.*?\]0$"),                              "不要"),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_layer_rule(layer_name: str) -> str | None:
    """Rule-based classification only. Returns category or None."""
    for pat, cat in _RULES:
        if pat.search(layer_name):
            return cat
    return None


def classify_layers(
    layers: list[dict[str, Any]],
    *,
    use_llm: bool = True,
    cache_path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Classify a batch of layers.

    Strategy (LLM-first, rules as fallback):
      1. Hit the on-disk cache by layer name — every uncached layer goes
         to the LLM. The fixed CATEGORIES list constrains the answer, but
         the actual layer → category mapping is the LLM's call. Rules
         can't enumerate every project's naming conventions, especially
         non-Takenaka files and IFC class names.
      2. If the LLM call degrades (no API key, SDK missing, network
         error), fall back to the rule table per-layer so the result is
         never empty. Only when neither LLM nor rule applies does a
         layer land at '不要'.

    Parameters
    ----------
    layers:
        List of dicts: ``{"name": str, "sample_texts": list[str], "type_count": dict}``
    use_llm:
        When False, skip the LLM entirely (rules-only).  Useful in tests
        and offline environments.
    cache_path:
        If provided, results are read from / written to this JSON file.

    Returns
    -------
    dict mapping layer name -> classification dict
    """
    cache: dict[str, dict[str, Any]] = {}
    if cache_path and cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
        # Drop entries whose category is no longer in CATEGORIES — handles
        # category renames / splits across versions. The dropped layers will
        # be re-classified on this call (LLM hit if available, else rules).
        cat_set = set(CATEGORIES)
        cache = {
            name: row
            for name, row in cache.items()
            if isinstance(row, dict) and row.get("category") in cat_set
        }

    out: dict[str, dict[str, Any]] = {}
    need_classify: list[dict[str, Any]] = []

    for L in layers:
        name = L["name"]
        if name in cache:
            out[name] = cache[name]
        else:
            need_classify.append(L)

    if need_classify:
        if use_llm:
            # LLM does the actual mapping — that's the whole point of having
            # one. The fixed CATEGORIES list is the constraint, but which
            # layer goes to which category is the model's call. Rules only
            # come back into play if the LLM call fails (no key, network
            # error, SDK missing).
            llm_results = _llm_classify_batch(need_classify)
            for name, res in llm_results.items():
                degraded = res.get("source", "").startswith(("no_", "llm_error"))
                if degraded:
                    rcat = classify_layer_rule(name)
                    # Guard against rules that return a category which no
                    # longer exists in CATEGORIES (happens after a category
                    # rename/removal). Treat as 不要 so the frontend doesn't
                    # receive an unrenderable bucket.
                    if rcat is not None and rcat not in CATEGORIES:
                        rcat = "不要"
                    if rcat is not None:
                        out[name] = {
                            "category": rcat,
                            "confidence": 0.9,
                            "source": "rule_fallback",
                        }
                    else:
                        out[name] = res  # keep the no_*/llm_error → 不要 row
                else:
                    out[name] = res
        else:
            # Explicitly rules-only mode (tests, offline environments).
            for L in need_classify:
                cat = classify_layer_rule(L["name"]) or "不要"
                out[L["name"]] = {
                    "category": cat,
                    "confidence": 0.9 if cat != "不要" else 0.0,
                    "source": "rule",
                }

    # Anything still uncovered → "不要"
    for L in layers:
        if L["name"] not in out:
            out[L["name"]] = {"category": "不要", "confidence": 0.0, "source": "default"}

    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache.update(out)
            cache_path.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    return out


# ---------------------------------------------------------------------------
# LLM batch classifier
# ---------------------------------------------------------------------------

def _llm_classify_batch(
    layers: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Send all unmatched layers to the LLM, chunked + parallel."""
    # Auto-load .env if present (project root or working dir).
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if not layers:
        return {}

    # Chunk to keep each LLM call fast and cheap. 30 layers per batch
    # finishes in ~10s with gpt-4o-mini and stays well under the model's
    # input context.
    BATCH = 30
    chunks = [layers[i:i + BATCH] for i in range(0, len(layers), BATCH)]

    if len(chunks) == 1:
        return _llm_classify_single_batch(chunks[0])

    # Run chunks in parallel via ThreadPoolExecutor (network-bound).
    from concurrent.futures import ThreadPoolExecutor
    out: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(chunks))) as ex:
        for partial in ex.map(_llm_classify_single_batch, chunks):
            out.update(partial)
    return out


# ---------------------------------------------------------------------------
# Static system prompt — same bytes for every batch + every parse.
# Lifting it to module scope lets prompt caching reuse the rendered prefix.
# ---------------------------------------------------------------------------

def _system_prompt() -> str:
    return (
        "あなたは日本のゼネコン施工図（DXF/IFC）を読むベテランエンジニアです。"
        "**スリーブ施工図のチェック**および**地図表示**にとって有用な要素だけを"
        "抽出するために、各レイヤーを分類してください。\n\n"
        "竹中工務店の標準レイヤー命名規則 (A211=室名, A221=記入文字, A858=スリーブ, "
        "C131=通心, C151=壁心, F102=RC柱, F104=RC梁, F108_3=スラブ段差, F108_5=床ヌスミ, "
        "F201=Ｓ柱, F203=ブレース, F305=梁ラベル, F308=スラブラベル, etc.) を熟知。\n\n"
        "**判定原則（最優先で適用）:**\n"
        "スリーブ施工図のチェックに必要なのは次の5系統のみ:\n"
        "  (1) スリーブ本体（位置・サイズ・系統・P-N番号）\n"
        "  (2) スラブ外形と FL 情報（段差線・スラブラベル・スラブFL）\n"
        "  (3) 躯体（躯体壁=RC・乾式壁=ALC/LGS等・耐火壁・柱・梁）と床ヌスミ\n"
        "  (4) 通り芯と寸法線\n"
        "  (5) 部屋名（位置の文脈用）\n"
        "上記5系統に **直接寄与しない** レイヤーはすべて '不要'。判定に迷ったら次を自問:\n"
        "  「このレイヤーが無くても、スリーブの位置/サイズ/FL/躯体干渉の14項目チェックは成立するか？」\n"
        "→ Yes なら '不要'。\n\n"
        "特に以下の系統は **他工種の加工指示・施工順序・図面装飾** であり、スリーブチェックには無関係 → '不要':\n"
        "  - 鉄骨側の加工指示: デッキ受け / デッキ受け切断 / デッキ受け部品 / デッキ受けハンチ / フランジ補強 / 鉄骨ジョイント\n"
        "  - 配筋詳細: 補強筋 / 主筋 / 鉄筋 / F160系\n"
        "  - 埋込物・装飾: 打込金物 / サッシアンカー / 面取り / 誘発目地 / コン止め / ハッチング\n"
        "  - 施工計画: 工区割り / 後打範囲 / 山留 / 仮設DW / 仮設スリーブ\n"
        "  - 図面ガジェット: 図面枠 / 表題欄 / 凡例 / ビューポート / 境界線 / 雲マーク / 注意点 / 修正履歴\n"
        "  - 詳細図参照: 階段詳細 / スロープ詳細 / 階段記号 / 方位記号 / 部分詳細 / 床仕上 / 仕上(細)\n"
        "  - その他: ANOTHER_WORKS / 別途工事 / オイルタンク詳細 / 0 / Defpoints / 空のシステムレイヤー\n\n"
        f"審査・地図表示に有用なカテゴリ:\n  {', '.join(USEFUL_CATEGORIES)}\n\n"
        "`category` は必ず上記カテゴリ + '不要' のどれか。リスト外は禁止。\n\n"
        "**有用カテゴリへの分類例**:\n"
        "- 'C131_通心' / '通り芯' / 'C141_通心記号' → '通り芯'\n"
        "- 'F105/F106_RC壁' / 'A421_壁:RC' / '★既存躯体外壁' / '[建築]壁' / 'F306_壁ラベル' → '躯体壁'\n"
        "- 'A422_壁:ALC' / 'A441_壁:LGS' / 'A442_壁:木軸' / 'A443_壁:CB' / 'A424_壁:パネル' / 'A423_壁:PCa' → '乾式壁'\n"
        "- 'A521_壁:仕上' / 'C151_壁心' → '不要' (仕上線・参照線はスリーブチェックに寄与しない)\n"
        "- 'A561_耐火被覆' / '複合耐火壁' / '防火区画線' / '区画壁' → '耐火壁・防火区画'\n"
        "- 'F102_RC柱' / 'F201_Ｓ柱' / 'F203_ブレース' / 'A412_柱:Ｓ' / 'エレベーター_間柱' → '柱・仕上線'\n"
        "- 'F104_RC梁' / 'F202_Ｓ梁' / 'F305_梁ラベル' → '梁'\n"
        "- 'F107_RC床' / 'F108_2_立上り' / 'F108_4_開口' / 'F112_基礎' / 'F401_ルーフドレン' / 'A311_吹抜' → 'スラブ外形'\n"
        "- 'F308_スラブラベル' (S16, 165t, -60 等) → 'スラブラベル'\n"
        "- 'F155_スラブレベル' (FL+40, FL-360, 350～300 等) → 'スラブFL'\n"
        "- 'F108_3_RCスラブ段差線' / '段差記号' → '段差線'\n"
        "- 'F108_5_床ヌスミ' → '床ヌスミ'\n"
        "- 'A221_記入文字' (1FL-565 等) / 'A223_レベル' → 'FL表記'\n"
        "- 'C161_通心寸法' / 'C162_その他寸法' / '配管寸' / '文字・寸法' → '寸法線'\n"
        "- 'A211_室名' (店舗1, 階段室, PS, シャフト等) → '部屋名'\n"
        "- '水勾配' → '水勾配'\n"
        "- '[衛生]通常' (P-N-x 並ぶ) → 'P-N番号'\n"
        "- '[衛生]雨水/汚水/ガス系' → '機器コード_衛生'\n"
        "- '[電気]通常/盤/非常照明/配置基準' → '機器コード_電気'\n"
        "- '[空調]通常/その他' → '機器コード_空調'\n"
        "- 'スリーブ' を含むレイヤー → 規律で 'スリーブ_衛生/空調/電気/その他'\n\n"
        "**IFC クラス名のマッピング**:\n"
        "- IfcWall* → '躯体壁' (RC/PCa) または '乾式壁' (ALC/LGS/木軸/CB) — Material/ObjectType で判定。耐火/防火/区画 があれば '耐火壁・防火区画'\n"
        "- IfcColumn → '柱・仕上線'\n"
        "- IfcBeam → '梁'\n"
        "- IfcSlab / IfcRoof / IfcFooting → 'スラブ外形'\n"
        "- IfcGrid / IfcGridAxis → '通り芯'\n"
        "- IfcOpeningElement / ProvisionForVoid / IfcBuildingElementProxy(スリーブ) → 'スリーブ_その他'\n"
        "- IfcFlowSegment / IfcDuct* (空調系) → '機器コード_空調'\n"
        "- IfcPipe* / IfcSanitary* (衛生系) → '機器コード_衛生'\n"
        "- IfcCableCarrier* / IfcElectric* → '機器コード_電気'\n"
        "- IfcSpace → '部屋名'\n"
        "- IfcAnnotation / IfcDimension / IfcDoor / IfcWindow → '不要'\n"
        "- IfcBuilding / IfcBuildingStorey / IfcSite / IfcProject → '不要'\n\n"
        "テキスト内容も補助的に使い、確信を高めてください "
        "(例: テキストに 'EW30(垂壁)' があれば '躯体壁'、'P-N-1' があれば 'P-N番号'、"
        "'FL+40' があれば 'スラブFL'、'S16 165t' のような表記があれば 'スラブラベル')。\n\n"
        "出力は厳密に以下の JSON のみ:\n"
        '{ "results": [ { "name": "...", "category": "...", "confidence": 0.0-1.0, "reason": "短い理由" } ] }'
    )


def _build_user_message(layers: list[dict[str, Any]]) -> str:
    items: list[dict[str, Any]] = []
    for L in layers:
        items.append({
            "name": L["name"],
            "types": L.get("type_count", {}),
            "sample_texts": L.get("sample_texts", [])[:8],
        })
    return "以下のレイヤーを分類してください:\n" + json.dumps(items, ensure_ascii=False, indent=2)


def _parse_results_json(text: str, layers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Parse the LLM's JSON output and validate every entry."""
    # The model is told to emit pure JSON; locate the outermost object even
    # if it added stray prose around it.
    text = text.strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]

    parsed = json.loads(text)
    if isinstance(parsed, dict) and "results" in parsed:
        arr = parsed["results"]
    elif isinstance(parsed, dict) and "layers" in parsed:
        arr = parsed["layers"]
    elif isinstance(parsed, list):
        arr = parsed
    else:
        arr = list(parsed.values())[0] if parsed else []

    result: dict[str, dict[str, Any]] = {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("layer")
        cat = item.get("category", "不要")
        if cat not in CATEGORIES:
            cat = "不要"
        if name:
            result[name] = {
                "category": cat,
                "confidence": float(item.get("confidence", 0.5)),
                "reason": str(item.get("reason", ""))[:200],
                "source": "llm",
            }
    # Backfill any layer the model didn't return
    for L in layers:
        if L["name"] not in result:
            result[L["name"]] = {
                "category": "不要",
                "confidence": 0.0,
                "source": "llm_missing",
            }
    return result


def _llm_classify_single_batch(
    layers: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """One LLM call for one chunk of layers.

    Anthropic Claude is preferred for accuracy. If ANTHROPIC_API_KEY is unset
    or the SDK is missing, fall back to OpenAI. If neither is available,
    every layer falls through to '不要'.
    """
    # Auto-load .env so the env-var checks below see the keys.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if os.getenv("ANTHROPIC_API_KEY"):
        return _classify_with_anthropic(layers)
    if os.getenv("OPENAI_API_KEY"):
        return _classify_with_openai(layers)
    return {
        L["name"]: {"category": "不要", "confidence": 0.0, "source": "no_llm"}
        for L in layers
    }


def _classify_with_anthropic(
    layers: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Anthropic Claude path. Uses prompt caching on the static system prompt."""
    try:
        import anthropic
    except ImportError:
        return {
            L["name"]: {"category": "不要", "confidence": 0.0, "source": "no_anthropic_sdk"}
            for L in layers
        }

    # claude-opus-4-7 is the default per claude-api skill — most capable
    # model, adaptive thinking only. Override via env if needed.
    model = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-7")
    client = anthropic.Anthropic()

    system_text = _system_prompt()
    user_text = _build_user_message(layers)

    # JSON Schema for structured outputs — Claude validates the response
    # shape against this, so we never get malformed JSON back.
    schema = {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "category": {"type": "string"},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": ["name", "category", "confidence"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["results"],
        "additionalProperties": False,
    }

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=4096,
            # System prompt is identical for every batch + every parse —
            # tag it for caching so subsequent invocations only pay ~0.1×
            # for the prefix.  Note: caches only kick in once the prompt
            # crosses ~4096 tokens on Opus-tier; smaller prompts silently
            # no-op (harmless).
            system=[{
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_text}],
            output_config={
                "effort": "high",
                "format": {"type": "json_schema", "schema": schema},
            },
        )
        text = next(
            (b.text for b in resp.content if getattr(b, "type", None) == "text"),
            "",
        )
        return _parse_results_json(text, layers)
    except Exception as e:
        return {
            L["name"]: {
                "category": "不要",
                "confidence": 0.0,
                "source": f"llm_error:anthropic:{type(e).__name__}",
            }
            for L in layers
        }


def _classify_with_openai(
    layers: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """OpenAI fallback. Used only if ANTHROPIC_API_KEY is unset."""
    try:
        from openai import OpenAI
    except ImportError:
        return {
            L["name"]: {"category": "不要", "confidence": 0.0, "source": "no_openai_sdk"}
            for L in layers
        }

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI()

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": _build_user_message(layers)},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        return _parse_results_json(content, layers)
    except Exception as e:
        return {
            L["name"]: {
                "category": "不要",
                "confidence": 0.0,
                "source": f"llm_error:openai:{type(e).__name__}",
            }
            for L in layers
        }
