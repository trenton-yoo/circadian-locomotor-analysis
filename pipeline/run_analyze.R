#!/usr/bin/env Rscript
# CLI: analyze a preprocessed job with confirmed parameters.
# Usage: Rscript pipeline/run_analyze.R <job_dir> <params.json>

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
  stop("Usage: Rscript pipeline/run_analyze.R <job_dir> <params.json>")
}

job_dir <- args[[1]]
params_path <- args[[2]]

suppressPackageStartupMessages({
  library(dplyr)
  library(tidyr)
  library(lubridate)
  library(stringr)
  library(tibble)
  library(writexl)
  library(jsonlite)
})

project_root <- Sys.getenv("LOCO_PROJECT_ROOT")
if (!nzchar(project_root)) {
  project_root <- normalizePath(getwd(), winslash = "/", mustWork = FALSE)
}
pipeline_dir <- file.path(project_root, "pipeline")
setwd(project_root)

source(file.path(pipeline_dir, "analyze.R"))

rds_path <- file.path(job_dir, "preprocessed.rds")
if (!file.exists(rds_path)) stop("Missing preprocessed.rds in job dir.")
if (!file.exists(params_path)) stop("Missing params.json")

pre <- readRDS(rds_path)
params <- fromJSON(params_path, simplifyVector = TRUE)

tryCatch({
  animal_meta <- pre$animal_meta

  # Apply edited labels from UI (list of {animal_id, animal_label})
  if (!is.null(params$labels) && length(params$labels) > 0) {
    lab_df <- as_tibble(params$labels)
    if (!"animal_id" %in% names(lab_df)) stop("labels must include animal_id")
    for (i in seq_len(nrow(lab_df))) {
      aid <- as.integer(lab_df$animal_id[i])
      idx <- which(animal_meta$animal_id == aid)
      if (length(idx) == 1) {
        val <- lab_df$animal_label[i]
        if (is.null(val) || is.na(val) || !nzchar(as.character(val))) {
          animal_meta$animal_label[idx] <- NA_character_
        } else {
          animal_meta$animal_label[idx] <- as.character(val)
        }
      }
    }
  }

  thresholds <- as.numeric(params$threshold)
  if (length(thresholds) == 0 || any(is.na(thresholds))) {
    stop("Invalid threshold in params.json")
  }

  min_episode_minutes <- as.numeric(params$min_episode_minutes)
  if (is.na(min_episode_minutes)) stop("Invalid min_episode_minutes")

  start_date <- as.Date(params$start_date)
  if (is.na(start_date)) stop("Invalid start_date")

  output_xlsx <- file.path(job_dir, "wake_episodes.xlsx")

  result <- analyze_locomotor(
    preprocessed = pre,
    animal_meta = animal_meta,
    start_date = start_date,
    thresholds = thresholds,
    min_episode_minutes = min_episode_minutes,
    bin_minutes = pre$bin_minutes,
    tz_out = pre$tz_out %||% "UTC",
    output_xlsx = output_xlsx
  )

  # Summary table for workspace: avg episode length by animal × period
  summary_out <- result$episodes_summary %>%
    transmute(
      animal_id,
      cage_code = ifelse(is.na(cage_code), "", as.character(cage_code)),
      animal_label = ifelse(is.na(animal_label), "", as.character(animal_label)),
      period,
      threshold,
      total_wake_episodes,
      total_wake_minutes,
      avg_episode_length = round(avg_episode_length, 2)
    )

  out <- list(
    status = "ok",
    output_file = "wake_episodes.xlsx",
    bin_minutes = result$bin_minutes,
    thresholds = result$thresholds,
    min_episode_minutes = result$min_episode_minutes,
    start_date = as.character(result$start_date),
    summary = lapply(seq_len(nrow(summary_out)), function(i) as.list(summary_out[i, , drop = FALSE]))
  )

  write(
    toJSON(out, auto_unbox = TRUE, pretty = TRUE, null = "null", na = "null"),
    file.path(job_dir, "summary.json")
  )
  message("Analyze OK → ", output_xlsx)
}, error = function(e) {
  err <- list(status = "error", message = conditionMessage(e))
  write(
    toJSON(err, auto_unbox = TRUE, pretty = TRUE),
    file.path(job_dir, "summary.json")
  )
  message("Analyze FAILED: ", conditionMessage(e))
  quit(status = 1)
})
