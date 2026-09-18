import os
import sys
import json
import joblib
import numpy as np
import pandas as pd
from datetime import datetime

# Ensure backend root is on sys.path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_ROOT = os.path.dirname(CURRENT_DIR)
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

from sklearn.model_selection import train_test_split, StratifiedKFold, cross_validate
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.tree import DecisionTreeClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score,
    precision_score,
    recall_score,
    f1_score,
    confusion_matrix,
    classification_report,
)
from sklearn.pipeline import Pipeline

from ml.dataset import get_training_data, FEATURE_COLUMNS


def run_training_tournament():
    print("=" * 70)
    print("WATERWATCH MAASIN CITY: MACHINE LEARNING MODEL TRAINING PIPELINE")
    print("Capstone Research Study - Southern Leyte State University")
    print("=" * 70)

    # 1. Load data
    df, X, y = get_training_data()

    # Save dataset CSV for manuscript reproduction
    csv_path = os.path.join(CURRENT_DIR, "water_quality_dataset.csv")
    df.to_csv(csv_path, index=False)
    print(f"\n[Artifact] Saved training dataset to: {csv_path}")

    # 2. Stratified Train / Test Split (80% Train, 20% Test)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.20, random_state=42, stratify=y
    )
    print(f"\nSplit: {len(X_train)} training samples, {len(X_test)} testing samples.")

    # 3. Define Model Tournament Candidates
    models = {
        "Logistic Regression (Linear Baseline)": Pipeline([
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(max_iter=1000, random_state=42)),
        ]),
        "Decision Tree (Single Tree Baseline)": Pipeline([
            ("clf", DecisionTreeClassifier(max_depth=5, random_state=42)),
        ]),
        "Random Forest (Proposed Ensemble)": Pipeline([
            (
                "clf",
                RandomForestClassifier(
                    n_estimators=100,
                    max_depth=6,
                    min_samples_split=4,
                    min_samples_leaf=2,
                    random_state=42,
                ),
            ),
        ]),
    }

    # 4. 5-Fold Stratified Cross-Validation Comparison
    print("\n" + "-" * 70)
    print("PHASE 1: 5-FOLD STRATIFIED CROSS-VALIDATION TOURNAMENT")
    print("-" * 70)
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    scoring = ["accuracy", "precision", "recall", "f1"]

    cv_results = {}
    for name, pipeline in models.items():
        scores = cross_validate(pipeline, X_train, y_train, cv=cv, scoring=scoring)
        cv_results[name] = {
            "mean_accuracy": float(np.mean(scores["test_accuracy"])),
            "std_accuracy": float(np.std(scores["test_accuracy"])),
            "mean_precision": float(np.mean(scores["test_precision"])),
            "mean_recall": float(np.mean(scores["test_recall"])),
            "mean_f1": float(np.mean(scores["test_f1"])),
        }
        print(f"\nModel: {name}")
        print(f"  CV Accuracy:  {cv_results[name]['mean_accuracy'] * 100:.2f}% (± {cv_results[name]['std_accuracy'] * 100:.2f}%)")
        print(f"  CV Precision: {cv_results[name]['mean_precision'] * 100:.2f}%")
        print(f"  CV Recall:    {cv_results[name]['mean_recall'] * 100:.2f}%")
        print(f"  CV F1-Score:  {cv_results[name]['mean_f1'] * 100:.2f}%")

    # 5. Train & Evaluate on Held-Out Test Set
    print("\n" + "-" * 70)
    print("PHASE 2: HELD-OUT TEST EVALUATION (20% UNSEEN VALIDATION SET)")
    print("-" * 70)

    test_evaluations = {}
    best_pipeline = None

    for name, pipeline in models.items():
        pipeline.fit(X_train, y_train)
        y_pred = pipeline.predict(X_test)

        acc = float(accuracy_score(y_test, y_pred))
        prec = float(precision_score(y_test, y_pred, zero_division=0))
        rec = float(recall_score(y_test, y_pred, zero_division=0))
        f1 = float(f1_score(y_test, y_pred, zero_division=0))
        cm = confusion_matrix(y_test, y_pred).tolist()

        test_evaluations[name] = {
            "accuracy": acc,
            "precision": prec,
            "recall": rec,
            "f1_score": f1,
            "confusion_matrix": cm,
            "tn": cm[0][0],
            "fp": cm[0][1],
            "fn": cm[1][0],
            "tp": cm[1][1],
        }

        print(f"\n{name}:")
        print(f"  Test Accuracy:  {acc * 100:.2f}%")
        print(f"  Test Precision: {prec * 100:.2f}%")
        print(f"  Test Recall:    {rec * 100:.2f}%")
        print(f"  Test F1-Score:  {f1 * 100:.2f}%")
        print(f"  Confusion Matrix: [TN={cm[0][0]}, FP={cm[0][1]}], [FN={cm[1][0]}, TP={cm[1][1]}]")

        if "Random Forest" in name:
            best_pipeline = pipeline

    # 6. Feature Importance Extraction from Random Forest
    rf_clf = best_pipeline.named_steps["clf"]
    importances = rf_clf.feature_importances_
    feat_importance_list = []
    for feat_name, imp in sorted(zip(FEATURE_COLUMNS, importances), key=lambda x: x[1], reverse=True):
        feat_importance_list.append({
            "feature": feat_name,
            "importance": float(imp),
            "percentage": round(float(imp) * 100, 2),
        })

    print("\n" + "-" * 70)
    print("PHASE 3: RANDOM FOREST FEATURE IMPORTANCE RANKING")
    print("-" * 70)
    for idx, item in enumerate(feat_importance_list, 1):
        print(f"  {idx}. {item['feature']:<30} {item['percentage']:>6.2f}%")

    # 7. Serialize Artifacts
    model_filename = os.path.join(CURRENT_DIR, "water_quality_model.pkl")
    model_payload = {
        "pipeline": best_pipeline,
        "feature_columns": FEATURE_COLUMNS,
        "trained_at": datetime.now().isoformat(),
        "model_type": "RandomForestClassifier",
        "hyperparameters": rf_clf.get_params(),
        "test_metrics": test_evaluations["Random Forest (Proposed Ensemble)"],
        "feature_importance": feat_importance_list,
    }
    joblib.dump(model_payload, model_filename)
    print(f"\n[Artifact] Saved serialized model to: {model_filename}")

    metrics_filename = os.path.join(CURRENT_DIR, "model_evaluation_metrics.json")
    full_report = {
        "study": "Participatory GIS & Random Forest Water Quality Early Warning",
        "timestamp": datetime.now().isoformat(),
        "total_samples": len(df),
        "feature_count": len(FEATURE_COLUMNS),
        "features": FEATURE_COLUMNS,
        "cross_validation_5fold": cv_results,
        "held_out_test_metrics": test_evaluations,
        "feature_importances": feat_importance_list,
    }
    with open(metrics_filename, "w") as f:
        json.dump(full_report, f, indent=2)
    print(f"[Artifact] Saved manuscript metrics JSON to: {metrics_filename}")

    print("\n" + "=" * 70)
    print("TRAINING PIPELINE SUCCESSFULLY COMPLETED!")
    print("=" * 70)
    return full_report


if __name__ == "__main__":
    run_training_tournament()
