---
name: deployment-checklist
description: |
  TS Transformer の Stage1 Optuna 完了後から Stage2 Optuna、ONNX export、release 配置、miniPC 検証までをチェックリスト化した正式手順。
  gate_score_threshold 未計算、hmm_regime.joblib 未同梱、旧 v5 release の取り違えを防ぎたいときに使用。
argument-hint: "[release-dir or study/trial]"
---

# Deployment Checklist

## 目的

Stage1 の best trial をそのまま本番へ持ち込むのではなく、

1. OOS 予測を回収する
2. gate-free の `ml_trades` を作る
3. Stage2 Optuna で Exit と `gate_score_threshold` を確定する
4. その成果物を release dir に束ねる
5. miniPC 側で同じ release を読む

この順を必ず守る。`Stage1 -> Stage2 Optuna -> export -> deploy` の鎖を飛ばすな。

## 使う主なスクリプト

| Step | Script | 読むもの | 出すもの |
|---|---|---|---|
| Stage1 Optuna | `scripts/single_regression_transformer_cpcv_worker.py` | features, labels, Optuna DB | Stage1 study / best trial |
| OOS回収 | `scripts/collect_ts_transformer_oos.py` | features, labels, Stage1 study | `oos_predictions_ts.parquet`, `ts_best_trial.json`, `fold_boundaries_ts.json` |
| ml_trades生成 | `scripts/generate_ml_trades_ts.py` | `oos_predictions_ts.parquet`, features | `ml_trades_ts.csv`, `metadata_trades.json` |
| Stage2 Optuna | `scripts/optimize_stage2_exit_cpcv.py` | `ml_trades_ts.csv`, `fold_boundaries_ts.json`, price/tick | Stage2 study, `stage2_best_params.json`, `hmm_regime.joblib`, `regime_exit_strategies.json` |
| Release export | `scripts/export_ts_transformer_to_onnx.py` | Stage1 study/trial, Stage2 params/artifacts, OOS parquet | release dir 一式 |
| 本番起動 | `scripts/onnx_ts_trader.py` | release dir | realtime trader |

## フロー図

```text
Stage1 Optuna
  single_regression_transformer_cpcv_worker.py --target-mode ts
    ↓ best trial番号を確定
collect_ts_transformer_oos.py
    ↓
  oos_predictions_ts.parquet
  fold_boundaries_ts.json
    ↓
generate_ml_trades_ts.py --gate-top-q 1.0
    ↓
  ml_trades_ts.csv   # gate-free
    ↓
optimize_stage2_exit_cpcv.py --mode ts
    ↓
  stage2_best_params.json
  hmm_regime.joblib
  regime_exit_strategies.json
    ↓
export_ts_transformer_to_onnx.py
    ↓
  release dir
    ↓
onnx_ts_trader.py --model-dir <release dir>
```

## 事前確認

- [ ] 実行は `python3` 直叩きではなく `.venv/bin/python` を使う
- [ ] features と labels の期間・index が一致している
- [ ] Stage1 study 名と trial 番号をメモした
- [ ] release dir 名を新規に決めた
  例: `v7_ts_transformer_t94_v2`
- [ ] 旧 release dir (`v5_ts_transformer` など) を出力先に再利用しない

## Step 1: Stage1 best trial を確定

TS 用 Stage1 は `single_regression_transformer_cpcv_worker.py --target-mode ts` を使う。

```bash
cd /home/shogo/projects/FX_autotrader

.venv/bin/python scripts/single_regression_transformer_cpcv_worker.py \
  --features /home/shogo/projects/Models/Features/features_transformer.parquet \
  --labels data/labels/labels_latest.parquet \
  --target-mode ts \
  --storage "$OPTUNA_STORAGE_STAGE1" \
  --study-name sr_transformer_ts_v9
```

完了条件:

- [ ] Optuna DB に TS study が存在する
- [ ] deploy 候補の trial 番号が決まった
- [ ] `study-name` / `trial-number` を次工程へ引き継げる

## Step 2: OOS predictions と fold 境界を固定

Stage2 はここで作る `fold_boundaries_ts.json` を使う。Stage1 の fold と同期していない Stage2 は使うな。

```bash
.venv/bin/python scripts/collect_ts_transformer_oos.py \
  --features /home/shogo/projects/Models/Features/features_transformer.parquet \
  --labels data/labels/labels_latest.parquet \
  --storage "$OPTUNA_STORAGE_STAGE1" \
  --study-name sr_transformer_ts_v9 \
  --trial-number 94 \
  --output-dir results/ts_transformer_oos_t94
```

出力:

- `results/ts_transformer_oos_t94/oos_predictions_ts.parquet`
- `results/ts_transformer_oos_t94/ts_best_trial.json`
- `results/ts_transformer_oos_t94/fold_boundaries_ts.json`

完了条件:

- [ ] `oos_predictions_ts.parquet` が存在する
- [ ] `fold_boundaries_ts.json` が存在する
- [ ] `ts_best_trial.json` の `best_trial_number` が想定 trial と一致する

## Step 3: gate-free の ml_trades を生成

`generate_ml_trades_ts.py` の既定値 `--gate-top-q 0.0505` は Stage2 入力には不適切。
Stage2 自身が `gate_top_q` を最適化するゆえ、ここでは **必ず `--gate-top-q 1.0`** を指定する。

```bash
.venv/bin/python scripts/generate_ml_trades_ts.py \
  --oos-predictions results/ts_transformer_oos_t94/oos_predictions_ts.parquet \
  --features /home/shogo/projects/Models/Features/features_transformer.parquet \
  --gate-top-q 1.0 \
  --output-dir results/ts_transformer_oos_t94
```

出力:

- `results/ts_transformer_oos_t94/ml_trades_ts.csv`
- `results/ts_transformer_oos_t94/metadata_trades.json`

完了条件:

- [ ] `ml_trades_ts.csv` が存在する
- [ ] `metadata_trades.json` の `gate_top_q` が `1.0` である
- [ ] `ml_trades_ts.csv` に `ensemble_gate_score` 列がある

## Step 4: Stage2 Optuna を回す

`optimize_stage2_exit_cpcv.py` は `ml_trades.csv (gate-free)` と `fold_boundaries.json` を必須とする。
TS deploy 用の `gate_score_threshold` は、この Step で `_export_deploy_config()` が `ml_trades` 分布から事前計算して `stage2_best_params.json` へ書き出す。

```bash
.venv/bin/python scripts/optimize_stage2_exit_cpcv.py \
  --trade-log results/ts_transformer_oos_t94/ml_trades_ts.csv \
  --fold-boundaries results/ts_transformer_oos_t94/fold_boundaries_ts.json \
  --symbol USD_JPY \
  --timeframe M5 \
  --mode ts \
  --study-name stage2_ts_exit_v23_t94 \
  --storage "$OPTUNA_STORAGE_STAGE2" \
  --n-trials 1000 \
  --output-dir results/stage2_ts_exit_v23_t94
```

出力:

- `results/stage2_ts_exit_v23_t94/stage2_best_params.json`
- `results/stage2_ts_exit_v23_t94/hmm_regime.joblib`
- `results/stage2_ts_exit_v23_t94/regime_exit_strategies.json`

完了条件:

- [ ] `stage2_best_params.json` の `best_params.gate_score_threshold` が正の有限値
- [ ] `best_params.regime_mode` が `hmm_2state` または意図通りの値
- [ ] `hmm_regime.joblib` が存在する
- [ ] `regime_exit_strategies.json` が存在する

## Step 5: release dir を export する

HMM deploy の正式経路は file-based flow である。すなわち Stage2 の成果物を引数で明示して export する。

```bash
.venv/bin/python scripts/export_ts_transformer_to_onnx.py \
  --features /home/shogo/projects/Models/Features/features_transformer.parquet \
  --labels data/labels/labels_latest.parquet \
  --storage "$OPTUNA_STORAGE_STAGE1" \
  --study-name sr_transformer_ts_v9 \
  --trial-number 94 \
  --oos-predictions results/ts_transformer_oos_t94/oos_predictions_ts.parquet \
  --regime-mode hmm_2state \
  --stage2-params results/stage2_ts_exit_v23_t94/stage2_best_params.json \
  --regime-model results/stage2_ts_exit_v23_t94/hmm_regime.joblib \
  --regime-strategies results/stage2_ts_exit_v23_t94/regime_exit_strategies.json \
  --output-dir /home/shogo/projects/Models/releases/v7_ts_transformer_t94_v2
```

release dir 必須ファイル:

- `ts_transformer.onnx`
- `scaler.pkl`
- `nan_medians.json`
- `live_feature_columns.txt`
- `feature_manifest.json`
- `metadata.json`
- `stage2_best_params.json`
- `ts_inference_config.json`
- `hmm_regime.joblib`
- `regime_exit_strategies.json`

完了条件:

- [ ] 上記 10 ファイルが揃う
- [ ] `stage2_best_params.json` と `ts_inference_config.json` の `stage1_trial` / `stage2_trial` が一致する
- [ ] `stage2_best_params.json.best_params.regime_model.artifact_path == "hmm_regime.joblib"`

## Step 6: Ubuntu 側 release 検証

- [ ] `metadata.json` の `deployment_version` が新 release 名である
- [ ] `stage2_best_params.json.best_params.gate_score_threshold > 0`
- [ ] `ts_inference_config.json.gate_score_threshold` が `stage2_best_params.json` と一致する
- [ ] `feature_manifest.json.n_features` と `live_feature_columns.txt` 行数が一致する
- [ ] `hmm_regime.joblib` の存在を `ls` で確認した

簡易確認:

```bash
python - <<'PY'
import json, pathlib
root = pathlib.Path("/home/shogo/projects/Models/releases/v7_ts_transformer_t94_v2")
s2 = json.loads((root / "stage2_best_params.json").read_text())
cfg = json.loads((root / "ts_inference_config.json").read_text())
bp = s2["best_params"]
print("stage1_trial:", cfg["stage1_trial"])
print("stage2_trial:", cfg["stage2_trial"], s2["stage2_trial"])
print("gate:", cfg["gate_score_threshold"], bp["gate_score_threshold"])
print("regime_mode:", bp["regime_mode"])
print("hmm exists:", (root / "hmm_regime.joblib").exists())
PY
```

## Step 7: miniPC 配置

- [ ] miniPC 側へ **新 release dir だけ** を転送する
- [ ] trader 起動時の `--model-dir` が新 release dir を指す
- [ ] 旧 `v5_ts_transformer` を参照していない

例:

```bash
python scripts/onnx_ts_trader.py \
  --symbol USDJPY \
  --model-dir Models/releases/v7_ts_transformer_t94_v2 \
  --dry-run
```

完了条件:

- [ ] trader が `stage2_best_params.json` を読んで起動する
- [ ] `regime_mode=hmm_2state` で `hmm_regime.joblib` 読込に失敗しない
- [ ] gate 表示値が release の `gate_score_threshold` と一致する

## 障害分析

### 1. `gate_score_threshold` が未計算だった原因

- 旧運用では OOS からの閾値計算が手順依存で、release 前に materialize されない場合があった
- 現行 TS の正規経路では `optimize_stage2_exit_cpcv.py` の `_export_deploy_config()` が `ml_trades` の `ensemble_gate_score` から静的閾値を計算し、`stage2_best_params.json` に保存する
- よって原因は「Stage2 export まで完走していない」か「gate-free でない `ml_trades` を渡した」かのいずれかである

### 2. `hmm_regime.joblib` が出力されなかった原因

- HMM artifact は Stage2 側の `_save_hmm_deploy_artifact()` か、export 側の `copy_regime_files()` を通らねば release に入らない
- 旧 v5 型の export は HMM file を要求せず、release に 7 ファイルしかなかった
- したがって原因は「HMM 対応前の古い export 手順を再使用した」または「`--regime-model` / `--regime-strategies` を渡さなかった」ことである

### 3. `ts_inference_config.json` に v5 の値が残った原因

- 既存 `ts-deployment-v1` には `v5_ts_transformer` 固定の例や `gate_score_threshold: null` を含む旧記述が残っている
- 実際に旧 release dir `v5_ts_transformer` と新 release dir `v7_ts_transformer_t94_v2` は `stage2_study`, `stage2_trial`, `gate_score_threshold` が異なる
- ゆえに根本原因は「新 release を新規ディレクトリへ出さず、旧 v5 前提の path / 文書 / 起動引数を引きずった」ことである

## 自動化提案

1. `prepare_ts_release.py` を作り、Step 2-5 を一括実行させる
2. `ml_trades_ts.csv` 生成時に `--gate-top-q 1.0` 以外なら即 fail させる
3. release dir に `release_manifest.json` を追加し、Stage1/Stage2 study・trial・source path を記録する
4. trader 起動前に `model_dir` が旧 `v5_*` を指していれば fail する preflight を入れる
5. `verify_ts_release.py` を追加し、必須 10 ファイルと trial/gate/hmm 整合を自動検査する

## やってはならぬこと

- Stage2 前の `ml_trades` を pre-filter したまま使うな
- `generate_ml_trades_ts.py` の既定 `0.0505` を鵜呑みにするな
- `stage2_best_params.json` 未生成のまま export するな
- `hmm_regime.joblib` 不在で `regime_mode=hmm_2state` を名乗るな
- `v5_ts_transformer` を新 release の置き換え先として再利用するな
