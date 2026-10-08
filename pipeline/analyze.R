# =====================================================
# analyze.R
# Confirmed params + preprocessed data → wake episodes Excel
# =====================================================

# Expected packages: dplyr, tidyr, lubridate, writexl, tibble

`%||%` <- function(x, y) if (is.null(x)) y else x

get_period <- function(datetime) {
  clock_min <- lubridate::hour(datetime) * 60 + lubridate::minute(datetime)

  day1_start  <- 19 * 60 + 5   # 7:05 PM
  day1_end    <- 19 * 60 + 35  # 7:35 PM
  night_start <- 19 * 60 + 40  # 7:40 PM
  night_end   <- 7 * 60 + 30   # 7:30 AM
  day2_start  <- 7 * 60 + 35   # 7:35 AM
  day2_end    <- 19 * 60       # 7:00 PM

  dplyr::case_when(
    (clock_min >= day1_start & clock_min <= day1_end) ~ "Day",
    (clock_min >= night_start | clock_min <= night_end) ~ "Night",
    (clock_min >= day2_start & clock_min <= day2_end) ~ "Day",
    TRUE ~ NA_character_
  )
}

detect_episodes <- function(df, threshold, bin_minutes) {
  if (nrow(df) == 0) return(tibble::tibble())

  df <- df %>%
    dplyr::arrange(time_fixed) %>%
    dplyr::mutate(active = counts >= threshold)

  n <- nrow(df)
  new_run <- c(
    TRUE,
    (df$active[-1] != df$active[-n]) | (df$period[-1] != df$period[-n])
  )
  df$run_id <- cumsum(new_run)

  df %>%
    dplyr::group_by(run_id) %>%
    dplyr::summarise(
      animal = dplyr::first(animal),
      animal_id = dplyr::first(animal_id),
      cage_code = dplyr::first(cage_code),
      animal_label = dplyr::first(animal_label),
      period = dplyr::first(period),
      start_time = dplyr::first(time_fixed),
      end_time = dplyr::last(time_fixed),
      duration_bins = dplyr::n(),
      duration_minutes = dplyr::n() * bin_minutes,
      total_counts = sum(counts, na.rm = TRUE),
      active_flag = dplyr::first(active),
      .groups = "drop"
    ) %>%
    dplyr::filter(active_flag) %>%
    dplyr::select(
      animal, animal_id, cage_code, animal_label, period,
      start_time, end_time, duration_bins, duration_minutes, total_counts
    )
}

#' Analyze preprocessed locomotor data with confirmed parameters.
#'
#' @param preprocessed List returned by preprocess_locomotor()
#' @param animal_meta Metadata table (possibly with edited labels)
#' @param start_date Date data collection began
#' @param thresholds Activity count threshold(s)
#' @param min_episode_minutes Drop episodes shorter than this
#' @param bin_minutes Bin size; defaults to preprocessed$value
#' @param tz_out Timezone for POSIXct
#' @param output_xlsx Output path
#' @return List with final_full, df_long, episodes_all, episodes_summary,
#'   overall_mean_lengths, output_xlsx
analyze_locomotor <- function(
  preprocessed,
  animal_meta = preprocessed$animal_meta,
  start_date,
  thresholds = 100,
  min_episode_minutes = 10,
  bin_minutes = preprocessed$bin_minutes,
  tz_out = preprocessed$tz_out %||% "UTC",
  output_xlsx = "wake_episodes.xlsx"
) {
  thresholds <- as.numeric(thresholds)
  if (any(is.na(thresholds)) || length(thresholds) == 0) {
    stop("`thresholds` must be one or more numbers.")
  }
  min_episode_minutes <- as.numeric(min_episode_minutes)
  if (is.na(min_episode_minutes) || min_episode_minutes < 0) {
    stop("`min_episode_minutes` must be a non-negative number.")
  }
  bin_minutes <- as.integer(bin_minutes)
  start_date <- as.Date(start_date)
  if (is.na(start_date)) stop("`start_date` is missing or invalid.")

  keep_secs <- preprocessed$keep_secs
  counts <- preprocessed$counts

  message("\n===== Settings for analysis =====")
  message("Threshold(s): ", paste(thresholds, collapse = ", "))
  message("Min episode length: ", min_episode_minutes, " minutes")
  message("Start date: ", start_date)
  message("Bin size: ", bin_minutes, " minutes")

  # Build continuous POSIXct with midnight day rollover
  wrap_flags <- c(FALSE, keep_secs[-1] < keep_secs[-length(keep_secs)])
  day_offset_within <- cumsum(wrap_flags)
  time_fixed <- as.POSIXct(start_date, tz = tz_out) +
    lubridate::days(day_offset_within) +
    lubridate::seconds(keep_secs)

  final_full <- dplyr::bind_cols(
    tibble::tibble(time_fixed = time_fixed),
    counts
  )

  message("\nFirst 6 rows:")
  print(utils::head(final_full, 6))
  message("Last 6 rows:")
  print(utils::tail(final_full, 6))

  if (any(duplicated(final_full$time_fixed))) {
    warning("Duplicated time_fixed values present after analysis time build.")
  }

  final_full <- final_full %>%
    dplyr::mutate(period = get_period(time_fixed))

  message("\nPeriod counts:")
  print(table(final_full$period, useNA = "ifany"))

  df_long <- final_full %>%
    tidyr::pivot_longer(
      cols = tidyselect::starts_with("animal_"),
      names_to = "animal",
      values_to = "counts"
    ) %>%
    dplyr::left_join(
      dplyr::select(animal_meta, column_name, animal_id, cage_code, animal_label),
      by = c("animal" = "column_name")
    ) %>%
    dplyr::arrange(animal_id, time_fixed)

  stopifnot(all(c("time_fixed", "animal", "counts", "period") %in% names(df_long)))

  message(
    "df_long rows: ", nrow(df_long),
    " (= ", nrow(final_full), " bins × ", nrow(animal_meta), " animals)"
  )
  message("Rows per animal:")
  print(dplyr::count(df_long, animal_id, animal))

  episodes_all <- list()
  for (thresh in thresholds) {
    message("Processing threshold = ", thresh, " (bin = ", bin_minutes, " min)")
    eps_list <- df_long %>%
      dplyr::group_split(animal) %>%
      lapply(function(subdf) {
        detect_episodes(subdf, threshold = thresh, bin_minutes = bin_minutes)
      })
    episodes_all[[as.character(thresh)]] <- dplyr::bind_rows(eps_list) %>%
      dplyr::mutate(threshold = thresh)
  }
  episodes_all <- dplyr::bind_rows(episodes_all)

  n_before <- nrow(episodes_all)
  episodes_all <- episodes_all %>%
    dplyr::filter(duration_minutes >= min_episode_minutes)
  message(
    "Min episode length filter: kept ", nrow(episodes_all), " / ", n_before,
    " episodes (duration >= ", min_episode_minutes, " min)"
  )

  episodes_all <- episodes_all %>%
    dplyr::arrange(animal_id, period, threshold, start_time) %>%
    dplyr::group_by(animal_id, period, threshold) %>%
    dplyr::mutate(episode_id = dplyr::row_number()) %>%
    dplyr::ungroup() %>%
    dplyr::arrange(animal_id, threshold, start_time)

  episodes_summary <- episodes_all %>%
    dplyr::group_by(animal, animal_id, cage_code, animal_label, period, threshold) %>%
    dplyr::summarise(
      total_wake_episodes = dplyr::n(),
      total_wake_minutes = sum(duration_minutes, na.rm = TRUE),
      avg_episode_length = mean(duration_minutes, na.rm = TRUE),
      .groups = "drop"
    )

  animals <- sort(unique(df_long$animal))
  periods <- c("Day", "Night")
  animal_lookup <- df_long %>%
    dplyr::distinct(animal, animal_id, cage_code, animal_label)

  episodes_summary <- tidyr::expand_grid(
    animal = animals,
    period = periods,
    threshold = thresholds
  ) %>%
    dplyr::left_join(animal_lookup, by = "animal") %>%
    dplyr::left_join(
      dplyr::select(episodes_summary, -animal_id, -cage_code, -animal_label),
      by = c("animal", "period", "threshold")
    ) %>%
    dplyr::mutate(
      total_wake_episodes = tidyr::replace_na(total_wake_episodes, 0),
      total_wake_minutes = tidyr::replace_na(total_wake_minutes, 0),
      avg_episode_length = tidyr::replace_na(avg_episode_length, 0)
    ) %>%
    dplyr::arrange(animal_id, period, threshold)

  overall_mean_lengths <- episodes_summary %>%
    dplyr::group_by(threshold, period) %>%
    dplyr::summarise(
      mean_avg_episode_length = mean(avg_episode_length, na.rm = TRUE),
      sd_avg_episode_length   = stats::sd(avg_episode_length, na.rm = TRUE),
      se_avg_episode_length   = stats::sd(avg_episode_length, na.rm = TRUE) / sqrt(dplyr::n()),
      .groups = "drop"
    ) %>%
    dplyr::arrange(threshold, period)

  writexl::write_xlsx(
    x = list(
      animal_metadata = dplyr::select(
        animal_meta, excel_col_letter, animal_id, cage_code, animal_label
      ),
      episodes = episodes_all,
      summary = episodes_summary,
      overall_means = overall_mean_lengths
    ),
    path = output_xlsx
  )
  message("Wrote results to: ", output_xlsx)

  list(
    final_full = final_full,
    df_long = df_long,
    episodes_all = episodes_all,
    episodes_summary = episodes_summary,
    overall_mean_lengths = overall_mean_lengths,
    animal_meta = animal_meta,
    output_xlsx = output_xlsx,
    thresholds = thresholds,
    min_episode_minutes = min_episode_minutes,
    bin_minutes = bin_minutes,
    start_date = start_date
  )
}
