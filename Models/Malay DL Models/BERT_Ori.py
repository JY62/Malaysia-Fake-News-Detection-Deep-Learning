import os
import ast
import numpy as np
import pandas as pd
import tensorflow as tf
import keras_tuner as kt
import malaya
from transformers import BertTokenizer
from tensorflow.keras import layers
from tensorflow.keras.optimizers import Adam
from tensorflow.keras.callbacks import EarlyStopping
from sklearn.utils import class_weight
from sklearn.model_selection import train_test_split
from sklearn.metrics import f1_score
from tqdm import tqdm

# Parameters
BATCH_SIZE = 8
MAX_SEQ_LENGTH = 512
EPOCHS = 50


class CustomSchedule(tf.keras.optimizers.schedules.LearningRateSchedule):
    def __init__(self, initial_learning_rate, warmup_steps, decay_steps):
        super().__init__()
        self.initial_learning_rate = tf.cast(initial_learning_rate, tf.float32)
        self.warmup_steps = tf.cast(warmup_steps, tf.float32)
        self.decay_steps = tf.cast(decay_steps, tf.float32)

    def __call__(self, step):
        step = tf.cast(step, tf.float32)
        # Warmup
        warmup_pct = tf.minimum(1.0, step / self.warmup_steps)
        warmup_lr = self.initial_learning_rate * warmup_pct
        # Decay
        decay_pct = tf.minimum(1.0, (step - self.warmup_steps) / self.decay_steps)
        decay_lr = self.initial_learning_rate * (1 - decay_pct)
        # Combine warmup and decay
        lr = tf.where(step < self.warmup_steps, warmup_lr, decay_lr)
        return tf.maximum(lr, self.initial_learning_rate * 0.1)  # Minimum learning rate

    def get_config(self):
        return {
            "initial_learning_rate": float(self.initial_learning_rate.numpy()),
            "warmup_steps": int(self.warmup_steps.numpy()),
            "decay_steps": int(self.decay_steps.numpy())
        }


def f1_m(y_true, y_pred):
    y_pred = tf.round(y_pred)
    tp = tf.reduce_sum(tf.cast(y_true * y_pred, 'float'), axis=0)
    fp = tf.reduce_sum(tf.cast((1 - y_true) * y_pred, 'float'), axis=0)
    fn = tf.reduce_sum(tf.cast(y_true * (1 - y_pred), 'float'), axis=0)

    precision = tp / (tp + fp + tf.keras.backend.epsilon())
    recall = tp / (tp + fn + tf.keras.backend.epsilon())
    f1 = 2 * precision * recall / (precision + recall + tf.keras.backend.epsilon())
    return tf.reduce_mean(f1)


def model_builder(hp):
    # Modify the input shape to include the sequence dimension
    embedding_input = layers.Input(shape=(None, 768), dtype=tf.float32, name='embedding_input')
    # Single attention layer
    attention = layers.MultiHeadAttention(
        num_heads=hp.Choice("num_attention_heads", values=[4, 6, 8]),
        key_dim=64
    )(embedding_input, embedding_input)
    # Pooling to reduce dimensionality
    attention = layers.GlobalAveragePooling1D()(attention)
    # Single projection layer
    combined = layers.Dense(
        hp.Choice("projection_dim", values=[128, 256]),
        activation='relu'
    )(attention)
    # Dense layers with regularization
    for i in range(hp.Int("num_dense_layers", 1, 2)):
        combined = layers.Dense(
            hp.Choice(f"dense_{i}_units", values=[128, 256]),
            activation='relu'
        )(combined)
        combined = layers.Dropout(hp.Float(f"dropout_{i}", 0.1, 0.3, step=0.1))(combined)
    output = layers.Dense(1, activation='sigmoid')(combined)
    # Compile the model
    model = tf.keras.Model(inputs=[embedding_input], outputs=[output])
    learning_rate = hp.Choice("learning_rate", values=[1e-5, 2e-5, 3e-5])
    optimizer = Adam(learning_rate=learning_rate)
    model.compile(
        optimizer=optimizer,
        loss='binary_crossentropy',
        metrics=[f1_m]
    )
    return model


# Define tokenization and chunking function
def tokenize_and_chunk(text, tokenizer, max_length=MAX_SEQ_LENGTH):
    # Tokenize the text, including special tokens
    tokenized = tokenizer.encode(text, add_special_tokens=True)

    # Adjust max length to account for [CLS] and [SEP]
    adjusted_max_length = max_length - 10  # Reserve space for [CLS] and [SEP]

    # Chunk the tokenized sequence
    chunks = [
        [tokenizer.cls_token_id] + tokenized[i:i + adjusted_max_length] + [tokenizer.sep_token_id]
        for i in range(0, len(tokenized), adjusted_max_length)
    ]
    return chunks


# Combine the 'Tokenized_Title' and 'Tokenized_Full_Context' columns
def combine_text(row):
    tokenized_title = ast.literal_eval(row['Tokenized_Title'])
    tokenized_full_context = ast.literal_eval(row['Tokenized_Full_Context'])
    # Add special tokens to differentiate title and content
    title_text = '[TITLE] ' + ' '.join(tokenized_title)
    content_text = ' [CONTENT] ' + ' '.join(tokenized_full_context)
    # Combine title and content
    combined_text = title_text + content_text
    # Tokenize and chunk combined text
    chunks = tokenize_and_chunk(combined_text, tokenizer)
    return chunks


# Flatten chunks into samples
def flatten_chunks(texts, labels):
    all_texts = []
    all_labels = []
    for text, label in zip(texts, labels):
        for chunk in text:
            all_texts.append(chunk)
            all_labels.append(label)

    # Ensure matching dimensions
    assert len(all_texts) == len(all_labels), "Mismatch between chunks and labels"
    return all_texts, all_labels


# Aggregate predictions by averaging probabilities for each original sample
def aggregate_predictions(text_chunks, predictions):
    aggregated_predictions = []
    idx = 0
    for chunks in text_chunks:
        num_chunks = len(chunks)  # Number of chunks in this text
        aggregated_predictions.append(np.mean(predictions[idx:idx + num_chunks]))
        idx += num_chunks
    return aggregated_predictions


def vectorize_text(texts, labels, filename, batch_size=100):
    embeddings = []
    all_labels = []
    num_texts = len(texts)

    for i, (text, label) in enumerate(zip(tqdm(texts, desc="Embedding Progress"), labels)):
        embedding = bert_model.vectorize([tokenizer.decode(text)])
        embeddings.append(embedding)
        all_labels.append(label)

        # Save embeddings and labels to disk in batches
        if (i + 1) % batch_size == 0 or i + 1 == num_texts:
            # Ensure both embeddings and labels are saved together
            batch_embeddings = np.array(embeddings, dtype=np.float32)
            batch_labels = np.array(all_labels, dtype=np.int32)  # Assuming labels are integers
            with open(filename, "ab") as f:
                np.save(f, batch_embeddings)
                np.save(f, batch_labels)
            embeddings = []  # Clear embeddings
            all_labels = []  # Clear labels to ensure the next batch starts fresh

    return all_labels


def load_embeddings_and_labels(file_path):
    embeddings = []
    labels = []
    with open(file_path, "rb") as f:
        while True:
            try:
                batch_embeddings = np.load(f)
                batch_labels = np.load(f)
                embeddings.append(batch_embeddings)
                labels.append(batch_labels)
            except EOFError:
                break
    return np.concatenate(embeddings, axis=0), np.concatenate(labels, axis=0)


# Load Malaya's BERT model
bert_model = malaya.transformer.huggingface(model='mesolitica/bert-base-standard-bahasa-cased')
tokenizer = BertTokenizer.from_pretrained('mesolitica/bert-base-standard-bahasa-cased')

# Load and preprocess data
file_path = os.path.join("..", "Dataset", "Processed_Dataset_BM.csv")
data = pd.read_csv(file_path)
data['combined_text'] = data.apply(combine_text, axis=1)
data['target'] = data['classification_result'].apply(lambda x: 1 if x == 'real' else 0)

# Split dataset
train_data, test_data = train_test_split(
    data,
    test_size=0.2,
    random_state=42,
    stratify=data['target']
)

# Prepare data
train_texts = train_data['combined_text'].tolist()
test_texts = test_data['combined_text'].tolist()
train_labels = train_data['target'].values
test_labels = test_data['target'].values

# # Flatten chunks to align text chunks with their corresponding labels
# train_chunks, train_chunk_labels = flatten_chunks(train_texts, train_labels)
# test_chunks, test_chunk_labels = flatten_chunks(test_texts, test_labels)
#
# # Convert chunks into embeddings
# print("Starting vectorization train dataset...")
# vectorize_text(train_chunks, train_chunk_labels, "train_bert_embeddings.npy")
# print("Starting vectorization test dataset...")
# vectorize_text(test_chunks, test_chunk_labels, "test_bert_embeddings.npy")

# Load saved embeddings and labels
train_embeddings, train_chunk_labels = load_embeddings_and_labels("train_bert_embeddings.npy")
test_embeddings, test_chunk_labels = load_embeddings_and_labels("test_bert_embeddings.npy")

# Validate dimensions
assert len(train_embeddings) == len(train_chunk_labels), \
    f"Train data mismatch: {len(train_embeddings)} embeddings vs {len(train_chunk_labels)} labels."
assert len(test_embeddings) == len(test_chunk_labels), \
    f"Test data mismatch: {len(test_embeddings)} embeddings vs {len(test_chunk_labels)} labels."

# Compute class weights
class_weights = class_weight.compute_class_weight(
    class_weight='balanced',
    classes=np.unique(train_chunk_labels),
    y=train_chunk_labels
)
class_weights = dict(enumerate(class_weights))
print("Class weights:", class_weights)

# Create datasets
train_ds = tf.data.Dataset.from_tensor_slices((train_embeddings, train_chunk_labels)) \
    .shuffle(10000) \
    .batch(BATCH_SIZE) \
    .prefetch(tf.data.AUTOTUNE)

test_ds = tf.data.Dataset.from_tensor_slices((test_embeddings, test_chunk_labels)) \
    .batch(BATCH_SIZE) \
    .prefetch(tf.data.AUTOTUNE)

# Callbacks
callbacks = [
    EarlyStopping(
        monitor='val_f1_m',
        patience=5,
        restore_best_weights=True,
        mode='max'
    )
]

# Initialize the tuner with Hyperband (adaptive, memory-efficient search)
tuner = kt.Hyperband(
    model_builder,
    objective=kt.Objective("val_f1_m", direction='max'),
    max_epochs=EPOCHS,
    factor=3,
    directory="bert_dir",
    project_name="bert"
)

# Search for best hyperparameters
tuner.search(train_ds,
             validation_data=test_ds,
             epochs=EPOCHS,
             callbacks=callbacks,
             class_weight=class_weights)

# Get the best model hyperparameters
best_hps = tuner.get_best_hyperparameters(num_trials=1)[0]

# Display the best hyperparameters
print("Best hyperparameters:", best_hps.values)

# Train the model with best hyperparameters
model = tuner.hypermodel.build(best_hps)
history = model.fit(
    train_ds,
    validation_data=test_ds,
    epochs=EPOCHS,
    callbacks=callbacks,
    class_weight=class_weights
)

# Predict probabilities for test chunks
chunk_predictions = model.predict(test_ds)

# Aggregate predictions for the original texts
original_lengths = [len(chunks) for chunks in test_texts]  # Number of chunks per text
aggregated_predictions = aggregate_predictions(test_texts, chunk_predictions)

# Convert probabilities to binary predictions
final_predictions = np.where(np.array(aggregated_predictions) > 0.5, 1, 0)

# Evaluate F1 score
f1 = f1_score(test_labels, final_predictions)
print(f"F1 Score: {f1}")

