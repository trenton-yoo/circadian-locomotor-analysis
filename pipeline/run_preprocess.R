#!/usr/bin/env Rscript
# CLI: preprocess an uploaded xlsx into a job directory.
# Usage: Rscript pipeline/run_preprocess.R <input_xlsx> <job_dir>

# Ensure user library is visible (jsonlite etc.)
user_lib <- Sys.getenv("R_LIBS_USER")
if (!nzchar(user_lib)) {
  user_lib <- path.expand(file.path(
    "~", "Library", "R", R.version$arch,
    paste(R.version$major, strsplit(R.version$minor, ".", fixed = TRUE)[[1]][1], sep = "."),
    "library"
  ))
}
if (dir.exists(user_lib)) .libPaths(c(user_lib, .libPaths()))

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 2) {
  stop("Usage: Rscript pipeline/run_preprocess.R <input_xlsx> <job_dir>")
}

input_xlsx <- args[[1]]
job_dir <- args[[2]]

suppressPackageStartupMessages({
  library(readxl)
  library(dplyr)
  library(tidyr)
  library(lubridate)
  library(stringr)
  library(tibble)
  library(jsonlite)
})

# Prefer explicit project root from the web app (handles spaces in paths).
project_root <- Sys.getenv("LOCO_PROJECT_ROOT")
if (!nzchar(project_root)) {
  project_root <- normalizePath(getwd(), winslash = "/", mustWork = FALSE)
}
pipeline_dir <- file.path(project_root, "pipeline")
setwd(project_root)

source(file.path(pipeline_dir, "preprocess.R"))

dir.create(job_dir, recursive = TRUE, showWarnings = FALSE)

tryCatch({
  pre <- preprocess_locomotor(
    input_xlsx = input_xlsx,
    sheet = 1,
    bin_minutes = NULL,
    start_date_fallback = as.Date("2020-01-01"),
    tz_out = "UTC"
  )

  saveRDS(pre, file.path(job_dir, "preprocessed.rds"))

  # First ~10 data rows for workspace preview
  n_preview <- min(10L, length(pre$keep_secs))
  time_labels <- vapply(
    pre$keep_secs[seq_len(n_preview)],
    fmt_clock,
    character(1)
  )
  preview_df <- dplyr::bind_cols(
    tibble::tibble(time = time_labels),
    pre$counts[seq_len(n_preview), , drop = FALSE]
  )

  preview <- list(
    status = "ok",
    input_file = basename(pre$input_xlsx),
    bin_minutes = pre$bin_minutes,
    suggested_start_date = as.character(pre$suggested_start_date),
    n_bins = pre$n_bins,
    first_clock = pre$first_clock,
    last_clock = pre$last_clock,
    n_midnight_wraps = pre$n_midnight_wraps,
    animal_meta = lapply(seq_len(nrow(pre$animal_meta)), function(i) {
      list(
        animal_id = pre$animal_meta$animal_id[i],
        cage_code = if (is.na(pre$animal_meta$cage_code[i])) "" else pre$animal_meta$cage_code[i],
        animal_label = if (is.na(pre$animal_meta$animal_label[i])) "" else pre$animal_meta$animal_label[i],
        column_name = pre$animal_meta$column_name[i],
        excel_col_letter = pre$animal_meta$excel_col_letter[i]
      )
    }),
    preview_columns = names(preview_df),
    preview_rows = lapply(seq_len(nrow(preview_df)), function(r) {
      as.list(preview_df[r, , drop = FALSE])
    })
  )

  write(
    toJSON(preview, auto_unbox = TRUE, pretty = TRUE, null = "null", na = "null"),
    file.path(job_dir, "preview.json")
  )
  message("Preprocess OK → ", job_dir)
}, error = function(e) {
  err <- list(status = "error", message = conditionMessage(e))
  write(
    toJSON(err, auto_unbox = TRUE, pretty = TRUE),
    file.path(job_dir, "preview.json")
  )
  message("Preprocess FAILED: ", conditionMessage(e))
  quit(status = 1)
})
