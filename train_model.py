import os

import joblib
import pandas as pd
from sklearn.metrics import accuracy_score, classification_report
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_FILE = os.path.join(BASE_DIR, 'smc_features_dataset.csv')
MODEL_FILE = os.path.join(BASE_DIR, 'smc_svm_model.pkl')

# Must match the feature order backtest.py sends to model.predict().
FEATURE_COLUMNS = ['ob_height', 'volume_ratio', 'impulse_size', 'has_fvg']
TARGET_COLUMN = 'outcome'


def train_model():
    print("Loading SMC feature dataset...")
    if not os.path.exists(DATASET_FILE):
        print("Dataset not found. Run `python backtest.py --collect` first.")
        return
    data = pd.read_csv(DATASET_FILE)

    if len(data) < 10:
        print(f"Only {len(data)} labeled trades — not enough to train. "
              f"Run `python backtest.py --collect` over more data.")
        return
    if data[TARGET_COLUMN].nunique() < 2:
        print("Every trade has the same outcome — nothing to learn. "
              "Collect more data first.")
        return

    X = data[FEATURE_COLUMNS]
    y = data[TARGET_COLUMN]

    # Chronological split: rows are in time order, so train on the past
    # and test on the future (a random shuffle leaks look-ahead info).
    split = int(len(data) * 0.8)
    X_train, X_test = X.iloc[:split], X.iloc[split:]
    y_train, y_test = y.iloc[:split], y.iloc[split:]
    if y_train.nunique() < 2:
        print("Training slice has only one outcome class — collect more data.")
        return

    # SVMs are scale-sensitive (ob_height is in the hundreds, volume_ratio
    # ~1), so features are standardised inside the pipeline and saved with
    # the model.
    model = make_pipeline(StandardScaler(), SVC(kernel='rbf', class_weight='balanced'))
    model.fit(X_train, y_train)

    predictions = model.predict(X_test)
    print(f"\nTrain rows: {len(X_train)}, test rows: {len(X_test)}")
    print(f"Model Accuracy: {accuracy_score(y_test, predictions) * 100:.2f}%\n")
    print(classification_report(y_test, predictions, zero_division=0))

    joblib.dump(model, MODEL_FILE)
    print(f"[+] Model saved successfully as '{os.path.basename(MODEL_FILE)}'")


if __name__ == '__main__':
    train_model()
