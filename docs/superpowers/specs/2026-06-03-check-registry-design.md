# チェックレジストリ + AI生成 — 設計仕様

日付: 2026-06-03
ステータス: 承認済み（実装着手）

## 目的

現在 `sleeve_checker/checks.py` にハードコードされている14観点を、**画面から
見える・編集できる・自由に追加できる**チェック定義の集合（レジストリ）に
作り替える。ユーザーは自由文で基準を書き、LLM がそこからチェックの Python を
生成して自動適用し、従来通り ○/× を出す。

## 決定事項（ユーザー確認済み）

- 編集方式: **AI生成方式**（自由文 → コード生成）
- 対象範囲: **フリーに追加可能**（14観点に縛られない汎用レジストリ）
- 保存範囲: **システム全体で共通**（永続ディスクに1つ）
- 承認フロー: **自動適用**（生成プレビューなし）
- 生成LLM: OpenAI `gpt-4o-mini`（`.env` 既設、`layer_classifier.py` と同パターン）

## データモデル

```python
@dataclass
class CheckDef:
    id: int            # 連番。組込み 1..14、新規 15..
    name: str          # 表示名
    category: str      # "整合性" / "施工図表現" / "その他"
    description: str   # 自由文の基準（ユーザー編集対象）
    builtin_key: str | None  # 組込み関数キー。None = AI生成
    code: str | None         # 生成Python（builtin_key があるときは None）
    enabled: bool
    order: int
```

- `check_id` は**数値のまま**（フロント変更を最小化）。
- 保存先: `DATA_DIR/checks/registry.json`（システム全体で1ファイル）。

## 初期データ（seed）

初回起動時、既存14観点を `builtin_key` 付きで投入。`description` は既存コードの
`rule` テキストから流用。**組込みは文字列 exec せず元の関数を直接呼ぶ**ので
精度劣化ゼロ。触らない限り今と完全に同じ挙動。

組込みキー → 既存 `checks.py` 関数の対応:

| id | name | builtin_key | scope |
|----|------|-------------|-------|
| 2 | 設備種別記載 | discipline | sleeve |
| 3 | 口径・外径記載 | diameter_label | sleeve |
| 4 | 寸法合計 | dim_sum | global |
| 5 | 勾配記載 | gradient | sleeve |
| 6 | 下階壁干渉 | lower_wall | sleeve(縦のみ) |
| 7 | 段差近接 | step_slab | sleeve |
| 8 | 基準レベル記載 | base_level | global(横のみ) |
| 9 | 位置特定寸法 | position_determinacy | global |
| 10 | 段差基準寸法 | step_dim | sleeve |
| 11 | スリーブ芯寸法 | sleeve_center_dim | global |
| 12 | 柱・壁仕上寸法 | column_wall_dim | sleeve |
| 13 | 寸法表記統一 | dim_notation | global |
| 14 | スリーブ番号記載 | sleeve_number | sleeve |

## 統一インターフェース

全チェック（組込み・生成とも）は次の形で実行される:

```python
def check(floor, ctx) -> list[CheckResult]:
    ...
```

- `floor`: チェック対象階の `FloorData`
- `ctx`: `CheckContext` — 共有状態 + ヘルパー
  - `ctx.lower_floor: FloorData | None`
  - `ctx.wall_thickness: dict[str, float]`
  - `ctx.position_determinacy()` — #9/#11 が共有する解決結果をメモ化
  - 幾何ヘルパー: `point_to_segment_distance`, `points_match`,
    `point_in_polygon`, `point_on_any_segment`
  - `re`, `math`
  - `ctx.result(...)` — `CheckResult` を作るヘルパー（severity/message 等）

組込みは `builtin_key` → アダプタ関数（`ctx` から正しい引数を取り出して既存
`checks.py` 関数を呼ぶ薄いラッパ）。#9 と #11 の依存（`x_resolved`/`y_resolved`）
は `ctx.position_determinacy()` のメモ化で解消。

ランナーは各 def の結果に `check_id=def.id` / `check_name=def.name` を上書きして
グルーピングの一貫性を保つ。

## AI生成（codegen）

`sleeve_checker/codegen.py`:

- `generate_check_code(name, description) -> str`
- OpenAI `gpt-4o-mini`、`temperature=0`。システムプロンプトに以下を渡す:
  - `FloorData` / `Sleeve` 等の全フィールド定義
  - 利用可能なヘルパー（`ctx.*`、幾何関数、`re`/`math`）
  - `CheckResult` の形と `ctx.result(...)` の使い方
  - 既存チェック1〜2件を `def check(floor, ctx)` 形式に書き直した few-shot
  - 出力は **Python コードのみ**（` ```python ` フェンスは剥がす）
- 検証（生成後・保存前）:
  1. ` ```` ` フェンス除去
  2. `ast.parse` で構文チェック
  3. トップレベルに `def check(floor, ctx)` が存在
  4. 禁止ノードを AST 走査で拒否: `Import` / `ImportFrom` / 名前に `__` を含む
     属性・名前 / `open`/`exec`/`eval`/`compile`/`__import__`/`globals`/`locals`
     の呼び出し
- 検証失敗時は例外。API はその旨をエラーとして返し、保存しない。

## サンドボックス実行

`sleeve_checker/sandbox.py`（または registry 内）:

- 制限付き `__builtins__`（`len/range/min/max/abs/round/sorted/enumerate/zip/
  list/dict/set/tuple/float/int/str/bool/any/all/sum/isinstance/getattr` 等の
  安全サブセットのみ）+ ヘルパーを注入した名前空間で `exec`。
- `check(floor, ctx)` を呼ぶ。**各チェックを try/except で隔離** — 例外時は
  その観点だけ `severity="NG"`, `message="生成チェック実行エラー: <理由>"` を1件
  返す（全体は落とさない）。
- 初期版ではプロセスタイムアウトは入れない（将来課題）。社内・単一テナント前提。

## API（`api.py`）

- `GET /api/checks` → `[{id, name, category, description, source, enabled, order}]`
  （`source` = "builtin" | "generated"）
- `POST /api/checks` `{name, category, description}` → コード生成 → 追記 → 保存 →
  生成した def を返す（生成失敗は 400/500 でエラー詳細）
- `PUT /api/checks/{id}` `{name?, category?, description?, enabled?}`
  - `description` 変更時に再生成。組込みの description を変更したら
    `builtin_key=None`・`code=生成` に変換（= AI生成版へ置換）
  - `enabled` のみの変更は再生成しない
- `DELETE /api/checks/{id}` → 削除（組込みも削除可）
- `POST /api/check` → レジストリの **enabled な def を order 順**に実行して結果を返す
  （レスポンス形式は現状と同一: `{results, summary}`）

## フロントエンド

- `types.ts`: `CheckDef` 追加。`CheckResult.check_id` は number のまま。
- `api.ts`: `listChecks` / `createCheck` / `updateCheck` / `deleteCheck`。
- `components/CheckManager.tsx`（新規・モーダル）:
  - カテゴリ別一覧、各チェックの名前・基準文をインライン編集
  - ○/× トグル（enabled）、削除、`＋ 観点を追加`
  - 「保存して再生成」→ API 呼び出し、生成中スピナー
- `App.tsx`: ヘッダーに「チェック観点」ボタン → `CheckManager` を開く。
  保存後は既存の「再チェック」で実行。
- `ListView.tsx`: グループ seed を静的 `CHECK_DEFS` から **取得したレジストリ**に
  差し替え（新規チェックも全OKでも一覧に出る）。`openChecks` は number のまま。

## リスクと割り切り

- `gpt-4o-mini` は複雑な幾何ロジック（寸法チェーンBFS等）を一文から再現しにくい。
  → 組込み14観点は元コードを保持。AI生成はキーワード/テキスト系・単純な距離/
  有無判定で精度が出る。複雑な観点を文だけで作り替えると精度が落ち得ることを
  UI で注意表示する。
- 自動適用 + サーバでの生成コード実行はセキュリティ考慮点。
  → AST ホワイトリスト + 制限ビルトイン + try/except 隔離で緩和。

## テスト方針（TDD）

- `tests/test_check_registry.py`:
  - seed が空レジストリに14件を投入する
  - 組込みアダプタ実行結果が既存 `run_all_checks` と件数・severity で一致
    （回帰: 元の挙動を壊さない）
  - `position_determinacy()` メモ化が #9/#11 で共有される
- `tests/test_codegen_validation.py`:
  - 正常コードは通る / `import os` は拒否 / `__` 名は拒否 /
    `def check` 欠如は拒否 / フェンス除去
  - サンドボックスで例外チェックが「生成エラー」1件に化ける
- フロントは手動確認（チェック観点を開く→追加→再チェックで○×）。

## 完了後

機能完成・コミット後、`https://github.com/syawaryo/sekouzucheck.git` へ push。
（origin は既に新リポジトリに設定済み。）
