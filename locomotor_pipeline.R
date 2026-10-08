# =====================================================
# locomotor_pipeline.R
# Local terminal orchestrator:
#   preprocess → user parameter prompts → analyze
#
# Usage (from this project folder):
#   R
#   source("locomotor_pipeline.R")
# =====================================================

# Install once (uncomment if needed):
# install.packages(c(
#   "readxl", "dplyr", "tidyr", "lubridate", "stringr",
#   "purrr", "writexl", "tibble"
# ))

library(readxl)
library(dplyr)
library(tidyr)
library(lubridate)
library(stringr)
library(purrr)
library(tibble)
library(writexl)

# Load pipeline stages (paths relative to current working directory)
source("pipeline/preprocess.R")
source("pipeline/analyze.R")

# -------- CONFIG --------
# Path to Excel file. If NULL, uses the first *.xlsx in this folder
# (excluding wake_episodes.xlsx / sample_file.xlsx / temp files).
input_xlsx   <- "sample2.xlsx"
sheet        <- 1
tz_out       <- "UTC"
output_xlsx  <- "wake_episodes.xlsx"

# Bin size in minutes. NULL = auto-detect from consecutive timestamps.
bin_minutes  <- NULL

# Defaults used when not prompting (non-interactive) or if user presses Enter.
start_date_fallback <- as.Date("2020-01-01")
thresholds <- 100
min_episode_minutes <- 10

# If TRUE and running interactively, after preprocessing ask for:
# threshold, min episode length, start date, and optional animal_label edits.
ask_user_parameters <- TRUE
# ------------------------

parse_user_date <- function(s) {
  s <- str_trim(as.character(s))
  if (!nzchar(s) || is.na(s)) return(as.Date(NA))
  parsed <- suppressWarnings(parse_date_time(
    s,
    orders = c("Ymd", "mdy", "mdY", "dmy", "dmY", "m/d/y", "m/d/Y"),
    quiet = TRUE
  ))
  if (is.na(parsed)) return(as.Date(NA))
  as.Date(parsed)
}

prompt_with_default <- function(prompt, default) {
  default_txt <- if (is.na(default) || is.null(default)) "" else as.character(default)
  reply <- readline(sprintf("%s [%s]: ", prompt, default_txt))
  if (!nzchar(str_trim(reply))) return(default_txt)
  str_trim(reply)
}

# ---- 1) Preprocess ----
preprocessed <- preprocess_locomotor(
  input_xlsx = input_xlsx,
  sheet = sheet,
  bin_minutes = bin_minutes,
  start_date_fallback = start_date_fallback,
  tz_out = tz_out
)

animal_meta <- preprocessed$animal_meta
bin_minutes <- preprocessed$bin_minutes
file_start_date <- preprocessed$suggested_start_date

# ---- 2) Confirm / customize parameters (terminal UI stand-in) ----
if (isTRUE(ask_user_parameters) && interactive()) {
  message("\n===== Customize parameters (press Enter to keep default) =====")

  thresh_reply <- prompt_with_default(
    "Activity threshold count (comma-separated if multiple)",
    paste(thresholds, collapse = ",")
  )
  thresh_parsed <- suppressWarnings(as.numeric(str_split(thresh_reply, ",")[[1]]))
  thresh_parsed <- thresh_parsed[!is.na(thresh_parsed)]
  if (length(thresh_parsed) == 0) stop("No valid threshold entered.")
  thresholds <- thresh_parsed

  min_reply <- prompt_with_default(
    "Minimum episode length in minutes",
    min_episode_minutes
  )
  min_parsed <- suppressWarnings(as.numeric(min_reply))
  if (is.na(min_parsed) || min_parsed < 0) stop("Invalid minimum episode length.")
  min_episode_minutes <- min_parsed

  date_reply <- prompt_with_default(
    "Date data collection began (e.g. 2/12/25 or 2025-02-12)",
    preprocessed$suggested_start_date
  )
  date_parsed <- parse_user_date(date_reply)
  if (is.na(date_parsed)) stop("Could not parse start date: ", date_reply)
  file_start_date <- date_parsed

  message("\nCurrent animal labels:")
  print(animal_meta %>% select(animal_id, cage_code, animal_label), n = Inf)
  edit_reply <- tolower(str_trim(readline("Edit animal labels? (y/n) [n]: ")))
  if (edit_reply %in% c("y", "yes")) {
    message("For each animal: Enter=keep current, '-'=blank, or type a new label.")
    for (i in seq_len(nrow(animal_meta))) {
      cur <- animal_meta$animal_label[i]
      cur_disp <- if (is.na(cur) || !nzchar(cur)) "" else cur
      cage_disp <- if (is.na(animal_meta$cage_code[i])) "NA" else animal_meta$cage_code[i]
      ans <- readline(sprintf(
        "  Animal %s (cage %s) label [%s]: ",
        animal_meta$animal_id[i], cage_disp, cur_disp
      ))
      ans <- str_trim(ans)
      if (!nzchar(ans)) next
      if (ans == "-") {
        animal_meta$animal_label[i] <- NA_character_
      } else {
        animal_meta$animal_label[i] <- ans
      }
    }
    message("Updated animal labels:")
    print(animal_meta %>% select(animal_id, cage_code, animal_label), n = Inf)
  }

  confirm <- tolower(str_trim(readline("Proceed with analysis using these settings? (y/n): ")))
  if (!confirm %in% c("y", "yes")) stop("Aborted by user.")
} else if (isTRUE(ask_user_parameters) && !interactive()) {
  message("Non-interactive session: using CONFIG defaults (no prompts).")
}

# ---- 3) Analyze ----
result <- analyze_locomotor(
  preprocessed = preprocessed,
  animal_meta = animal_meta,
  start_date = file_start_date,
  thresholds = thresholds,
  min_episode_minutes = min_episode_minutes,
  bin_minutes = bin_minutes,
  tz_out = tz_out,
  output_xlsx = output_xlsx
)

# Expose main objects in the session (same names as before, for inspection)
final_full <- result$final_full
df_long <- result$df_long
episodes_all <- result$episodes_all
episodes_summary <- result$episodes_summary
overall_mean_lengths <- result$overall_mean_lengths
