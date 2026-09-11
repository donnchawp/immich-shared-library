--
-- PostgreSQL database dump
--

\restrict QHfP56dk7S2LHjpVMI2hzHCHmZIOEYbSzbZZjuvnT3g2EQrEaGNEAIomt7MG2Nu

-- Dumped from database version 14.19 (Debian 14.19-1.pgdg12+1)
-- Dumped by pg_dump version 14.19 (Debian 14.19-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: cube; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS cube WITH SCHEMA public;


--
-- Name: EXTENSION cube; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION cube IS 'data type for multidimensional cubes';


--
-- Name: earthdistance; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS earthdistance WITH SCHEMA public;


--
-- Name: EXTENSION earthdistance; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION earthdistance IS 'calculate great-circle distances on the surface of the Earth';


--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: unaccent; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public;


--
-- Name: EXTENSION unaccent; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION unaccent IS 'text search dictionary that removes accents';


--
-- Name: uuid-ossp; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public;


--
-- Name: EXTENSION "uuid-ossp"; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION "uuid-ossp" IS 'generate universally unique identifiers (UUIDs)';


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


--
-- Name: vchord; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vchord WITH SCHEMA public;


--
-- Name: EXTENSION vchord; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION vchord IS 'vchord: Vector database plugin for Postgres, written in Rust, specifically designed for LLM';


--
-- Name: album_user_role_enum; Type: TYPE; Schema: public; Owner: postgres
--

CREATE TYPE public.album_user_role_enum AS ENUM (
    'owner',
    'editor',
    'viewer'
);


ALTER TYPE public.album_user_role_enum OWNER TO postgres;

--
-- Name: asset_checksum_algorithm_enum; Type: TYPE; Schema: public; Owner: postgres
--

CREATE TYPE public.asset_checksum_algorithm_enum AS ENUM (
    'sha1',
    'sha1-path'
);


ALTER TYPE public.asset_checksum_algorithm_enum OWNER TO postgres;

--
-- Name: asset_visibility_enum; Type: TYPE; Schema: public; Owner: postgres
--

CREATE TYPE public.asset_visibility_enum AS ENUM (
    'archive',
    'timeline',
    'hidden',
    'locked'
);


ALTER TYPE public.asset_visibility_enum OWNER TO postgres;

--
-- Name: assets_status_enum; Type: TYPE; Schema: public; Owner: postgres
--

CREATE TYPE public.assets_status_enum AS ENUM (
    'active',
    'trashed',
    'deleted'
);


ALTER TYPE public.assets_status_enum OWNER TO postgres;

--
-- Name: sourcetype; Type: TYPE; Schema: public; Owner: postgres
--

CREATE TYPE public.sourcetype AS ENUM (
    'machine-learning',
    'exif',
    'manual'
);


ALTER TYPE public.sourcetype OWNER TO postgres;

--
-- Name: video_stream_variant_codec_enum; Type: TYPE; Schema: public; Owner: postgres
--

CREATE TYPE public.video_stream_variant_codec_enum AS ENUM (
    'av1',
    'hevc',
    'h264'
);


ALTER TYPE public.video_stream_variant_codec_enum OWNER TO postgres;

--
-- Name: album_asset_delete_audit(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.album_asset_delete_audit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      INSERT INTO album_asset_audit ("albumId", "assetId")
      SELECT "albumId", "assetId" FROM OLD
      WHERE "albumId" IN (SELECT "id" FROM album WHERE "id" IN (SELECT "albumId" FROM OLD));
      RETURN NULL;
    END
  $$;


ALTER FUNCTION public.album_asset_delete_audit() OWNER TO postgres;

--
-- Name: album_user_after_insert(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.album_user_after_insert() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      UPDATE album SET "updatedAt" = clock_timestamp(), "updateId" = immich_uuid_v7(clock_timestamp())
      WHERE "id" IN (SELECT "albumId" FROM inserted_rows)
        AND NOT EXISTS (SELECT FROM inserted_rows WHERE role = 'owner');
      RETURN NULL;
    END
  $$;


ALTER FUNCTION public.album_user_after_insert() OWNER TO postgres;

--
-- Name: album_user_delete(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.album_user_delete() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      DELETE FROM "album"
      WHERE "album"."id" = OLD."albumId"
      AND NOT EXISTS (SELECT "albumId" FROM "album_user" WHERE "album_user"."albumId" = "album"."id" AND "album_user"."role" = 'owner');

      RETURN NULL;
    END
  $$;


ALTER FUNCTION public.album_user_delete() OWNER TO postgres;

--
-- Name: album_user_delete_audit(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.album_user_delete_audit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      INSERT INTO album_audit ("albumId", "userId")
      SELECT "albumId", "userId"
      FROM OLD;

      IF pg_trigger_depth() = 1 THEN
        INSERT INTO album_user_audit ("albumId", "userId")
        SELECT "albumId", "userId"
        FROM OLD;
      END IF;

      RETURN NULL;
    END
  $$;


ALTER FUNCTION public.album_user_delete_audit() OWNER TO postgres;

--
-- Name: asset_delete_audit(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.asset_delete_audit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      INSERT INTO asset_audit ("assetId", "ownerId")
      SELECT "id", "ownerId"
      FROM OLD;
      RETURN NULL;
    END
  $$;


ALTER FUNCTION public.asset_delete_audit() OWNER TO postgres;

--
-- Name: asset_edit_audit(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.asset_edit_audit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      INSERT INTO asset_edit_audit ("editId", "assetId")
      SELECT "id", "assetId"
      FROM OLD;
      RETURN NULL;
    END
  $$;


ALTER FUNCTION public.asset_edit_audit() OWNER TO postgres;

--
-- Name: asset_edit_delete(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.asset_edit_delete() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      UPDATE asset
      SET "isEdited" = false
      FROM deleted_edit
      WHERE asset.id = deleted_edit."assetId" AND asset."isEdited"
        AND NOT EXISTS (SELECT FROM asset_edit edit WHERE edit."assetId" = asset.id);
      RETURN NULL;
    END
  $$;


ALTER FUNCTION public.asset_edit_delete() OWNER TO postgres;

--
-- Name: asset_edit_insert(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.asset_edit_insert() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      UPDATE asset
      SET "isEdited" = true
      FROM inserted_edit
      WHERE asset.id = inserted_edit."assetId" AND NOT asset."isEdited";
      RETURN NULL;
    END
  $$;


ALTER FUNCTION public.asset_edit_insert() OWNER TO postgres;

--
-- Name: asset_face_audit(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.asset_face_audit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      INSERT INTO asset_face_audit ("assetFaceId", "assetId")
      SELECT "id", "assetId"
      FROM OLD;
      RETURN NULL;
    END
  $$;


ALTER FUNCTION public.asset_face_audit() OWNER TO postgres;

--
-- Name: asset_metadata_audit(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.asset_metadata_audit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      INSERT INTO asset_metadata_audit ("assetId", "key")
      SELECT "assetId", "key"
      FROM OLD;
      RETURN NULL;
    END
  $$;


ALTER FUNCTION public.asset_metadata_audit() OWNER TO postgres;

--
-- Name: asset_ocr_delete_audit(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.asset_ocr_delete_audit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      INSERT INTO asset_ocr_audit ("assetId")
      SELECT "assetId"
      FROM OLD;
      RETURN NULL;
    END
  $$;


ALTER FUNCTION public.asset_ocr_delete_audit() OWNER TO postgres;

--
-- Name: f_concat_ws(text, text[]); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.f_concat_ws(text, text[]) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $_$SELECT array_to_string($2, $1)$_$;


ALTER FUNCTION public.f_concat_ws(text, text[]) OWNER TO postgres;

--
-- Name: f_unaccent(text); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.f_unaccent(text) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    RETURN public.unaccent('public.unaccent'::regdictionary, $1);


ALTER FUNCTION public.f_unaccent(text) OWNER TO postgres;

--
-- Name: immich_uuid_v7(timestamp with time zone); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.immich_uuid_v7(p_timestamp timestamp with time zone DEFAULT clock_timestamp()) RETURNS uuid
    LANGUAGE sql
    AS $$
    select encode(
      set_bit(
        set_bit(
          overlay(uuid_send(gen_random_uuid())
                  placing substring(int8send(floor(extract(epoch from p_timestamp) * 1000)::bigint) from 3)
                  from 1 for 6
          ),
          52, 1
        ),
        53, 1
      ),
      'hex')::uuid;
  $$;


ALTER FUNCTION public.immich_uuid_v7(p_timestamp timestamp with time zone) OWNER TO postgres;

--
-- Name: ll_to_earth_public(double precision, double precision); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.ll_to_earth_public(latitude double precision, longitude double precision) RETURNS public.earth
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
    SELECT public.cube(public.cube(public.cube(public.earth()*cos(radians(latitude))*cos(radians(longitude))),public.earth()*cos(radians(latitude))*sin(radians(longitude))),public.earth()*sin(radians(latitude)))::public.earth
  $$;


ALTER FUNCTION public.ll_to_earth_public(latitude double precision, longitude double precision) OWNER TO postgres;

--
-- Name: memory_asset_delete_audit(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.memory_asset_delete_audit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      INSERT INTO memory_asset_audit ("memoryId", "assetId")
      SELECT "memoriesId", "assetId" FROM OLD
      WHERE "memoriesId" IN (SELECT "id" FROM memory WHERE "id" IN (SELECT "memoriesId" FROM OLD));
      RETURN NULL;
    END
  $$;


ALTER FUNCTION public.memory_asset_delete_audit() OWNER TO postgres;

--
-- Name: memory_delete_audit(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.memory_delete_audit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      INSERT INTO memory_audit ("memoryId", "userId")
      SELECT "id", "ownerId"
      FROM OLD;
      RETURN NULL;
    END
  $$;


ALTER FUNCTION public.memory_delete_audit() OWNER TO postgres;

--
-- Name: partner_delete_audit(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.partner_delete_audit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      INSERT INTO partner_audit ("sharedById", "sharedWithId")
      SELECT "sharedById", "sharedWithId"
      FROM OLD;
      RETURN NULL;
    END
  $$;


ALTER FUNCTION public.partner_delete_audit() OWNER TO postgres;

--
-- Name: person_delete_audit(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.person_delete_audit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      INSERT INTO person_audit ("personGroupId", "ownerId")
      SELECT "personGroupId", "ownerId"
      FROM OLD;
      RETURN NULL;
    END
  $$;


ALTER FUNCTION public.person_delete_audit() OWNER TO postgres;

--
-- Name: person_group_delete_audit(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.person_group_delete_audit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      INSERT INTO person_group_audit ("personGroupId", "clusterGroupId")
      SELECT "id", "clusterGroupId"
      FROM OLD;
      RETURN NULL;
    END
  $$;


ALTER FUNCTION public.person_group_delete_audit() OWNER TO postgres;

--
-- Name: stack_delete_audit(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.stack_delete_audit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      INSERT INTO stack_audit ("stackId", "userId")
      SELECT "id", "ownerId"
      FROM OLD;
      RETURN NULL;
    END
  $$;


ALTER FUNCTION public.stack_delete_audit() OWNER TO postgres;

--
-- Name: updated_at(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    DECLARE
        clock_timestamp TIMESTAMP := clock_timestamp();
    BEGIN
        new."updatedAt" = clock_timestamp;
        new."updateId" = immich_uuid_v7(clock_timestamp);
        return new;
    END;
  $$;


ALTER FUNCTION public.updated_at() OWNER TO postgres;

--
-- Name: user_delete_audit(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.user_delete_audit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      INSERT INTO user_audit ("userId")
      SELECT "id"
      FROM OLD;
      RETURN NULL;
    END
  $$;


ALTER FUNCTION public.user_delete_audit() OWNER TO postgres;

--
-- Name: user_metadata_audit(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.user_metadata_audit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      INSERT INTO user_metadata_audit ("userId", "key")
      SELECT "userId", "key"
      FROM OLD;
      RETURN NULL;
    END
  $$;


ALTER FUNCTION public.user_metadata_audit() OWNER TO postgres;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: _face_sync_asset_map; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public._face_sync_asset_map (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_asset_id uuid NOT NULL,
    target_asset_id uuid NOT NULL,
    source_user_id uuid NOT NULL,
    target_user_id uuid NOT NULL,
    synced_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public._face_sync_asset_map OWNER TO postgres;

--
-- Name: _face_sync_meta; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public._face_sync_meta (
    key text NOT NULL,
    value text NOT NULL
);


ALTER TABLE public._face_sync_meta OWNER TO postgres;

--
-- Name: _face_sync_person_map; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public._face_sync_person_map (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_person_id uuid NOT NULL,
    target_person_id uuid NOT NULL,
    source_user_id uuid NOT NULL,
    target_user_id uuid NOT NULL
);


ALTER TABLE public._face_sync_person_map OWNER TO postgres;

--
-- Name: _face_sync_skipped; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public._face_sync_skipped (
    source_asset_id uuid NOT NULL,
    target_user_id uuid NOT NULL,
    reason text NOT NULL,
    skipped_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public._face_sync_skipped OWNER TO postgres;

--
-- Name: activity; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.activity (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    "albumId" uuid NOT NULL,
    "userId" uuid NOT NULL,
    "assetId" uuid,
    comment text,
    "isLiked" boolean DEFAULT false NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    CONSTRAINT activity_like_check CHECK ((((comment IS NULL) AND ("isLiked" = true)) OR ((comment IS NOT NULL) AND ("isLiked" = false))))
);


ALTER TABLE public.activity OWNER TO postgres;

--
-- Name: album; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.album (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "albumName" character varying DEFAULT 'Untitled Album'::character varying NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "albumThumbnailAssetId" uuid,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    description text,
    "deletedAt" timestamp with time zone,
    "isActivityEnabled" boolean DEFAULT true NOT NULL,
    "order" character varying DEFAULT 'desc'::character varying NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL
);


ALTER TABLE public.album OWNER TO postgres;

--
-- Name: COLUMN album."albumThumbnailAssetId"; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.album."albumThumbnailAssetId" IS 'Asset ID to be used as thumbnail';


--
-- Name: album_asset; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.album_asset (
    "albumId" uuid NOT NULL,
    "assetId" uuid NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL
);


ALTER TABLE public.album_asset OWNER TO postgres;

--
-- Name: album_asset_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.album_asset_audit (
    id uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "albumId" uuid NOT NULL,
    "assetId" uuid NOT NULL,
    "deletedAt" timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);


ALTER TABLE public.album_asset_audit OWNER TO postgres;

--
-- Name: album_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.album_audit (
    id uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "albumId" uuid NOT NULL,
    "userId" uuid NOT NULL,
    "deletedAt" timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);


ALTER TABLE public.album_audit OWNER TO postgres;

--
-- Name: album_user; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.album_user (
    "albumId" uuid NOT NULL,
    "userId" uuid NOT NULL,
    role public.album_user_role_enum DEFAULT 'editor'::public.album_user_role_enum NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    "createId" uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.album_user OWNER TO postgres;

--
-- Name: album_user_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.album_user_audit (
    id uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "albumId" uuid NOT NULL,
    "userId" uuid NOT NULL,
    "deletedAt" timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);


ALTER TABLE public.album_user_audit OWNER TO postgres;

--
-- Name: api_key; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.api_key (
    name character varying NOT NULL,
    key bytea NOT NULL,
    "userId" uuid NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    permissions character varying[] NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL
);


ALTER TABLE public.api_key OWNER TO postgres;

--
-- Name: asset; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.asset (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ownerId" uuid NOT NULL,
    type character varying NOT NULL,
    "originalPath" character varying NOT NULL,
    "fileCreatedAt" timestamp with time zone NOT NULL,
    "fileModifiedAt" timestamp with time zone NOT NULL,
    "isFavorite" boolean DEFAULT false NOT NULL,
    duration integer,
    checksum bytea NOT NULL,
    "livePhotoVideoId" uuid,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "originalFileName" character varying NOT NULL,
    thumbhash bytea,
    "isOffline" boolean DEFAULT false NOT NULL,
    "libraryId" uuid,
    "isExternal" boolean DEFAULT false NOT NULL,
    "deletedAt" timestamp with time zone,
    "localDateTime" timestamp with time zone NOT NULL,
    "stackId" uuid,
    "duplicateId" uuid,
    status public.assets_status_enum DEFAULT 'active'::public.assets_status_enum NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    visibility public.asset_visibility_enum DEFAULT 'timeline'::public.asset_visibility_enum NOT NULL,
    width integer,
    height integer,
    "isEdited" boolean DEFAULT false NOT NULL,
    "checksumAlgorithm" public.asset_checksum_algorithm_enum NOT NULL
);


ALTER TABLE public.asset OWNER TO postgres;

--
-- Name: asset_audio; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.asset_audio (
    "assetId" uuid NOT NULL,
    bitrate integer NOT NULL,
    index smallint NOT NULL,
    profile smallint,
    "codecName" text NOT NULL
);


ALTER TABLE public.asset_audio OWNER TO postgres;

--
-- Name: asset_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.asset_audit (
    id uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "assetId" uuid NOT NULL,
    "ownerId" uuid NOT NULL,
    "deletedAt" timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);


ALTER TABLE public.asset_audit OWNER TO postgres;

--
-- Name: asset_edit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.asset_edit (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "assetId" uuid NOT NULL,
    action character varying NOT NULL,
    parameters jsonb NOT NULL,
    sequence integer NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL
);


ALTER TABLE public.asset_edit OWNER TO postgres;

--
-- Name: asset_edit_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.asset_edit_audit (
    id uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "editId" uuid NOT NULL,
    "assetId" uuid NOT NULL,
    "deletedAt" timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);


ALTER TABLE public.asset_edit_audit OWNER TO postgres;

--
-- Name: asset_exif; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.asset_exif (
    "assetId" uuid NOT NULL,
    make character varying,
    model character varying,
    "exifImageWidth" integer,
    "exifImageHeight" integer,
    "fileSizeInByte" bigint,
    orientation character varying,
    "dateTimeOriginal" timestamp with time zone,
    "modifyDate" timestamp with time zone,
    "lensModel" character varying,
    "fNumber" double precision,
    "focalLength" double precision,
    iso integer,
    latitude double precision,
    longitude double precision,
    city character varying,
    state character varying,
    country character varying,
    description text DEFAULT ''::text NOT NULL,
    fps double precision,
    "exposureTime" character varying,
    "livePhotoCID" character varying,
    "timeZone" character varying,
    "projectionType" character varying,
    "profileDescription" character varying,
    colorspace character varying,
    "bitsPerSample" integer,
    "autoStackId" character varying,
    rating integer,
    "updatedAt" timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "lockedProperties" character varying[],
    tags character varying[]
);


ALTER TABLE public.asset_exif OWNER TO postgres;

--
-- Name: asset_face; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.asset_face (
    "assetId" uuid NOT NULL,
    "personGroupId" uuid,
    "imageWidth" integer DEFAULT 0 NOT NULL,
    "imageHeight" integer DEFAULT 0 NOT NULL,
    "boundingBoxX1" integer DEFAULT 0 NOT NULL,
    "boundingBoxY1" integer DEFAULT 0 NOT NULL,
    "boundingBoxX2" integer DEFAULT 0 NOT NULL,
    "boundingBoxY2" integer DEFAULT 0 NOT NULL,
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "sourceType" public.sourcetype DEFAULT 'machine-learning'::public.sourcetype NOT NULL,
    "deletedAt" timestamp with time zone,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "isVisible" boolean DEFAULT true NOT NULL
);


ALTER TABLE public.asset_face OWNER TO postgres;

--
-- Name: asset_face_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.asset_face_audit (
    id uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "assetFaceId" uuid NOT NULL,
    "assetId" uuid NOT NULL,
    "deletedAt" timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);


ALTER TABLE public.asset_face_audit OWNER TO postgres;

--
-- Name: asset_file; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.asset_file (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "assetId" uuid NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    type character varying NOT NULL,
    path character varying NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "isEdited" boolean DEFAULT false NOT NULL,
    "isProgressive" boolean DEFAULT false NOT NULL,
    "isTransparent" boolean DEFAULT false NOT NULL
);


ALTER TABLE public.asset_file OWNER TO postgres;

--
-- Name: asset_job_status; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.asset_job_status (
    "assetId" uuid NOT NULL,
    "facesRecognizedAt" timestamp with time zone,
    "metadataExtractedAt" timestamp with time zone,
    "duplicatesDetectedAt" timestamp with time zone,
    "ocrAt" timestamp with time zone
);


ALTER TABLE public.asset_job_status OWNER TO postgres;

--
-- Name: asset_keyframe; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.asset_keyframe (
    "assetId" uuid NOT NULL,
    pts integer[] NOT NULL,
    "accDuration" integer[] NOT NULL,
    "ownDuration" integer[] NOT NULL,
    "totalDuration" integer NOT NULL,
    "packetCount" integer NOT NULL,
    "outputFrames" integer NOT NULL
);


ALTER TABLE public.asset_keyframe OWNER TO postgres;

--
-- Name: asset_metadata; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.asset_metadata (
    "assetId" uuid NOT NULL,
    key character varying NOT NULL,
    value jsonb NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.asset_metadata OWNER TO postgres;

--
-- Name: asset_metadata_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.asset_metadata_audit (
    id uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "assetId" uuid NOT NULL,
    key character varying NOT NULL,
    "deletedAt" timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);


ALTER TABLE public.asset_metadata_audit OWNER TO postgres;

--
-- Name: asset_ocr; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.asset_ocr (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "assetId" uuid NOT NULL,
    x1 real NOT NULL,
    y1 real NOT NULL,
    x2 real NOT NULL,
    y2 real NOT NULL,
    x3 real NOT NULL,
    y3 real NOT NULL,
    x4 real NOT NULL,
    y4 real NOT NULL,
    "boxScore" real NOT NULL,
    "textScore" real NOT NULL,
    text text NOT NULL,
    "isVisible" boolean DEFAULT true NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.asset_ocr OWNER TO postgres;

--
-- Name: asset_ocr_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.asset_ocr_audit (
    id uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "assetId" uuid NOT NULL,
    "deletedAt" timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);


ALTER TABLE public.asset_ocr_audit OWNER TO postgres;

--
-- Name: asset_video; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.asset_video (
    "assetId" uuid NOT NULL,
    bitrate integer NOT NULL,
    "frameCount" integer NOT NULL,
    "timeBase" integer NOT NULL,
    index smallint NOT NULL,
    profile smallint,
    level smallint,
    "colorPrimaries" smallint NOT NULL,
    "colorTransfer" smallint NOT NULL,
    "colorMatrix" smallint NOT NULL,
    "dvProfile" smallint,
    "dvLevel" smallint,
    "dvBlSignalCompatibilityId" smallint,
    "codecName" text NOT NULL,
    "formatName" text NOT NULL,
    "formatLongName" text NOT NULL,
    "pixelFormat" text NOT NULL
);


ALTER TABLE public.asset_video OWNER TO postgres;

--
-- Name: cluster_group; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.cluster_group (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    name character varying,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL
);


ALTER TABLE public.cluster_group OWNER TO postgres;

--
-- Name: cluster_group_request; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.cluster_group_request (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "clusterGroupId" uuid NOT NULL,
    "userId" uuid NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.cluster_group_request OWNER TO postgres;

--
-- Name: face_search; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.face_search (
    "faceId" uuid NOT NULL,
    embedding public.vector(512) NOT NULL
);


ALTER TABLE public.face_search OWNER TO postgres;

--
-- Name: geodata_places; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.geodata_places (
    id integer NOT NULL,
    name character varying(200) NOT NULL,
    longitude double precision NOT NULL,
    latitude double precision NOT NULL,
    "countryCode" character(2) NOT NULL,
    "admin1Code" character varying(20),
    "admin2Code" character varying(80),
    "modificationDate" date NOT NULL,
    "admin1Name" character varying,
    "admin2Name" character varying,
    "alternateNames" character varying
);


ALTER TABLE public.geodata_places OWNER TO postgres;

--
-- Name: integrity_report; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.integrity_report (
    id uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    type character varying NOT NULL,
    path character varying NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "assetId" uuid,
    "fileAssetId" uuid
);


ALTER TABLE public.integrity_report OWNER TO postgres;

--
-- Name: kysely_migrations; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.kysely_migrations (
    name character varying(255) NOT NULL,
    "timestamp" character varying(255) NOT NULL
);


ALTER TABLE public.kysely_migrations OWNER TO postgres;

--
-- Name: kysely_migrations_lock; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.kysely_migrations_lock (
    id character varying(255) NOT NULL,
    is_locked integer DEFAULT 0 NOT NULL
);


ALTER TABLE public.kysely_migrations_lock OWNER TO postgres;

--
-- Name: library; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.library (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    name character varying NOT NULL,
    "ownerId" uuid NOT NULL,
    "importPaths" text[] NOT NULL,
    "exclusionPatterns" text[] NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    "deletedAt" timestamp with time zone,
    "refreshedAt" timestamp with time zone,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL
);


ALTER TABLE public.library OWNER TO postgres;

--
-- Name: memory; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.memory (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    "deletedAt" timestamp with time zone,
    "ownerId" uuid NOT NULL,
    type character varying NOT NULL,
    data jsonb NOT NULL,
    "isSaved" boolean DEFAULT false NOT NULL,
    "memoryAt" timestamp with time zone NOT NULL,
    "seenAt" timestamp with time zone,
    "showAt" timestamp with time zone,
    "hideAt" timestamp with time zone,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL
);


ALTER TABLE public.memory OWNER TO postgres;

--
-- Name: memory_asset; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.memory_asset (
    "memoriesId" uuid NOT NULL,
    "assetId" uuid NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL
);


ALTER TABLE public.memory_asset OWNER TO postgres;

--
-- Name: memory_asset_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.memory_asset_audit (
    id uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "memoryId" uuid NOT NULL,
    "assetId" uuid NOT NULL,
    "deletedAt" timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);


ALTER TABLE public.memory_asset_audit OWNER TO postgres;

--
-- Name: memory_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.memory_audit (
    id uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "memoryId" uuid NOT NULL,
    "userId" uuid NOT NULL,
    "deletedAt" timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);


ALTER TABLE public.memory_audit OWNER TO postgres;

--
-- Name: migration_overrides; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.migration_overrides (
    name character varying NOT NULL,
    value jsonb NOT NULL
);


ALTER TABLE public.migration_overrides OWNER TO postgres;

--
-- Name: move_history; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.move_history (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "entityId" uuid NOT NULL,
    "pathType" character varying NOT NULL,
    "oldPath" character varying NOT NULL,
    "newPath" character varying NOT NULL
);


ALTER TABLE public.move_history OWNER TO postgres;

--
-- Name: naturalearth_countries; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.naturalearth_countries (
    id integer NOT NULL,
    admin character varying(50) NOT NULL,
    admin_a3 character varying(3) NOT NULL,
    type character varying(50) NOT NULL,
    coordinates polygon NOT NULL
);


ALTER TABLE public.naturalearth_countries OWNER TO postgres;

--
-- Name: naturalearth_countries_tmp_id_seq1; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.naturalearth_countries ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.naturalearth_countries_tmp_id_seq1
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 2147483647
    CACHE 1
);


--
-- Name: notification; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.notification (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    "deletedAt" timestamp with time zone,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "userId" uuid,
    level character varying DEFAULT 'info'::character varying NOT NULL,
    type character varying DEFAULT 'info'::character varying NOT NULL,
    data jsonb,
    title character varying NOT NULL,
    description text,
    "readAt" timestamp with time zone
);


ALTER TABLE public.notification OWNER TO postgres;

--
-- Name: ocr_search; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.ocr_search (
    "assetId" uuid NOT NULL,
    text text NOT NULL
);


ALTER TABLE public.ocr_search OWNER TO postgres;

--
-- Name: partner; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.partner (
    "sharedById" uuid NOT NULL,
    "sharedWithId" uuid NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    "inTimeline" boolean DEFAULT false NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "createId" uuid DEFAULT public.immich_uuid_v7() NOT NULL
);


ALTER TABLE public.partner OWNER TO postgres;

--
-- Name: partner_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.partner_audit (
    id uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "sharedById" uuid NOT NULL,
    "sharedWithId" uuid NOT NULL,
    "deletedAt" timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);


ALTER TABLE public.partner_audit OWNER TO postgres;

--
-- Name: person; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.person (
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    "ownerId" uuid NOT NULL,
    name character varying DEFAULT ''::character varying NOT NULL,
    "thumbnailPath" character varying DEFAULT ''::character varying NOT NULL,
    "isHidden" boolean DEFAULT false NOT NULL,
    "birthDate" date,
    "faceAssetId" uuid,
    "isFavorite" boolean DEFAULT false NOT NULL,
    color character varying,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "personGroupId" uuid NOT NULL,
    CONSTRAINT "person_birthDate_chk" CHECK (("birthDate" <= CURRENT_DATE))
);


ALTER TABLE public.person OWNER TO postgres;

--
-- Name: person_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.person_audit (
    id uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "ownerId" uuid NOT NULL,
    "deletedAt" timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    "personGroupId" uuid NOT NULL
);


ALTER TABLE public.person_audit OWNER TO postgres;

--
-- Name: person_group; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.person_group (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "clusterGroupId" uuid NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "createId" uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL
);


ALTER TABLE public.person_group OWNER TO postgres;

--
-- Name: person_group_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.person_group_audit (
    id uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "personGroupId" uuid NOT NULL,
    "clusterGroupId" uuid NOT NULL,
    "deletedAt" timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);


ALTER TABLE public.person_group_audit OWNER TO postgres;

--
-- Name: plugin; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.plugin (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    name character varying NOT NULL,
    version character varying NOT NULL,
    title character varying NOT NULL,
    description character varying NOT NULL,
    author character varying NOT NULL,
    "wasmBytes" bytea NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    templates jsonb NOT NULL,
    sha256hash bytea NOT NULL
);


ALTER TABLE public.plugin OWNER TO postgres;

--
-- Name: plugin_method; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.plugin_method (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "pluginId" uuid NOT NULL,
    name character varying NOT NULL,
    title character varying NOT NULL,
    description character varying NOT NULL,
    types character varying[] NOT NULL,
    "hostFunctions" boolean DEFAULT false NOT NULL,
    "uiHints" character varying[] DEFAULT '{}'::character varying[] NOT NULL,
    schema jsonb,
    "allowedHosts" character varying[] DEFAULT '{}'::character varying[] NOT NULL
);


ALTER TABLE public.plugin_method OWNER TO postgres;

--
-- Name: session; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.session (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    token bytea NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    "userId" uuid NOT NULL,
    "deviceType" character varying DEFAULT ''::character varying NOT NULL,
    "deviceOS" character varying DEFAULT ''::character varying NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "pinExpiresAt" timestamp with time zone,
    "expiresAt" timestamp with time zone,
    "parentId" uuid,
    "isPendingSyncReset" boolean DEFAULT false NOT NULL,
    "appVersion" character varying,
    "oauthSid" character varying,
    "oauthBearerToken" character varying
);


ALTER TABLE public.session OWNER TO postgres;

--
-- Name: session_sync_checkpoint; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.session_sync_checkpoint (
    "sessionId" uuid NOT NULL,
    type character varying NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    ack character varying NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL
);


ALTER TABLE public.session_sync_checkpoint OWNER TO postgres;

--
-- Name: shared_link; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.shared_link (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    description character varying,
    "userId" uuid NOT NULL,
    key bytea NOT NULL,
    type character varying NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "expiresAt" timestamp with time zone,
    "allowUpload" boolean DEFAULT false NOT NULL,
    "albumId" uuid,
    "allowDownload" boolean DEFAULT true NOT NULL,
    "showExif" boolean DEFAULT true NOT NULL,
    password character varying,
    slug character varying
);


ALTER TABLE public.shared_link OWNER TO postgres;

--
-- Name: shared_link_asset; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.shared_link_asset (
    "assetId" uuid NOT NULL,
    "sharedLinkId" uuid NOT NULL
);


ALTER TABLE public.shared_link_asset OWNER TO postgres;

--
-- Name: smart_search; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.smart_search (
    "assetId" uuid NOT NULL,
    embedding public.vector(1152) NOT NULL
);


ALTER TABLE public.smart_search OWNER TO postgres;

--
-- Name: stack; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.stack (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "primaryAssetId" uuid NOT NULL,
    "ownerId" uuid NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL
);


ALTER TABLE public.stack OWNER TO postgres;

--
-- Name: stack_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.stack_audit (
    id uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "stackId" uuid NOT NULL,
    "userId" uuid NOT NULL,
    "deletedAt" timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);


ALTER TABLE public.stack_audit OWNER TO postgres;

--
-- Name: system_metadata; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.system_metadata (
    key character varying NOT NULL,
    value jsonb NOT NULL
);


ALTER TABLE public.system_metadata OWNER TO postgres;

--
-- Name: tag; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tag (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "userId" uuid NOT NULL,
    value character varying NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    color character varying,
    "parentId" uuid,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL
);


ALTER TABLE public.tag OWNER TO postgres;

--
-- Name: tag_asset; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tag_asset (
    "assetId" uuid NOT NULL,
    "tagId" uuid NOT NULL
);


ALTER TABLE public.tag_asset OWNER TO postgres;

--
-- Name: tag_closure; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tag_closure (
    id_ancestor uuid NOT NULL,
    id_descendant uuid NOT NULL
);


ALTER TABLE public.tag_closure OWNER TO postgres;

--
-- Name: user; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public."user" (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    email character varying NOT NULL,
    password character varying,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "profileImagePath" character varying DEFAULT ''::character varying NOT NULL,
    "isAdmin" boolean DEFAULT false NOT NULL,
    "shouldChangePassword" boolean DEFAULT true NOT NULL,
    "deletedAt" timestamp with time zone,
    "oauthId" character varying DEFAULT ''::character varying NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    "storageLabel" character varying,
    name character varying DEFAULT ''::character varying NOT NULL,
    "quotaSizeInBytes" bigint,
    "quotaUsageInBytes" bigint DEFAULT 0 NOT NULL,
    status character varying DEFAULT 'active'::character varying NOT NULL,
    "profileChangedAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "avatarColor" character varying,
    "pinCode" character varying,
    "clusterGroupId" uuid NOT NULL
);


ALTER TABLE public."user" OWNER TO postgres;

--
-- Name: user_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.user_audit (
    "userId" uuid NOT NULL,
    "deletedAt" timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    id uuid DEFAULT public.immich_uuid_v7() NOT NULL
);


ALTER TABLE public.user_audit OWNER TO postgres;

--
-- Name: user_metadata; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.user_metadata (
    "userId" uuid NOT NULL,
    key character varying NOT NULL,
    value jsonb NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.user_metadata OWNER TO postgres;

--
-- Name: user_metadata_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.user_metadata_audit (
    id uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    "userId" uuid NOT NULL,
    key character varying NOT NULL,
    "deletedAt" timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);


ALTER TABLE public.user_metadata_audit OWNER TO postgres;

--
-- Name: version_history; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.version_history (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    version character varying NOT NULL
);


ALTER TABLE public.version_history OWNER TO postgres;

--
-- Name: video_stream_segment; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.video_stream_segment (
    "variantId" uuid NOT NULL,
    index integer NOT NULL,
    "durationUs" integer NOT NULL
);


ALTER TABLE public.video_stream_segment OWNER TO postgres;

--
-- Name: video_stream_session; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.video_stream_session (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "assetId" uuid NOT NULL,
    "expiresAt" timestamp with time zone NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.video_stream_session OWNER TO postgres;

--
-- Name: video_stream_variant; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.video_stream_variant (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "sessionId" uuid NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    bitrate integer NOT NULL,
    codec public.video_stream_variant_codec_enum NOT NULL,
    resolution smallint NOT NULL
);


ALTER TABLE public.video_stream_variant OWNER TO postgres;

--
-- Name: workflow; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.workflow (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ownerId" uuid NOT NULL,
    trigger character varying NOT NULL,
    name character varying,
    description character varying,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updatedAt" timestamp with time zone DEFAULT now() NOT NULL,
    "updateId" uuid DEFAULT public.immich_uuid_v7() NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    logging boolean DEFAULT false NOT NULL
);


ALTER TABLE public.workflow OWNER TO postgres;

--
-- Name: workflow_log; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.workflow_log (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    "workflowId" uuid NOT NULL,
    result character varying NOT NULL,
    "workflowStepId" uuid,
    "triggerDataId" uuid,
    "runId" uuid NOT NULL
);


ALTER TABLE public.workflow_log OWNER TO postgres;

--
-- Name: workflow_step; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.workflow_step (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    "workflowId" uuid NOT NULL,
    "pluginMethodId" uuid NOT NULL,
    config jsonb,
    "order" integer NOT NULL
);


ALTER TABLE public.workflow_step OWNER TO postgres;

--
-- Name: move_history UQ_entityId_pathType; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.move_history
    ADD CONSTRAINT "UQ_entityId_pathType" UNIQUE ("entityId", "pathType");


--
-- Name: move_history UQ_newPath; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.move_history
    ADD CONSTRAINT "UQ_newPath" UNIQUE ("newPath");


--
-- Name: _face_sync_asset_map _face_sync_asset_map_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public._face_sync_asset_map
    ADD CONSTRAINT _face_sync_asset_map_pkey PRIMARY KEY (id);


--
-- Name: _face_sync_asset_map _face_sync_asset_map_source_target_user_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public._face_sync_asset_map
    ADD CONSTRAINT _face_sync_asset_map_source_target_user_key UNIQUE (source_asset_id, target_user_id);


--
-- Name: _face_sync_asset_map _face_sync_asset_map_target_asset_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public._face_sync_asset_map
    ADD CONSTRAINT _face_sync_asset_map_target_asset_id_key UNIQUE (target_asset_id);


--
-- Name: _face_sync_meta _face_sync_meta_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public._face_sync_meta
    ADD CONSTRAINT _face_sync_meta_pkey PRIMARY KEY (key);


--
-- Name: _face_sync_person_map _face_sync_person_map_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public._face_sync_person_map
    ADD CONSTRAINT _face_sync_person_map_pkey PRIMARY KEY (id);


--
-- Name: _face_sync_person_map _face_sync_person_map_source_person_id_target_user_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public._face_sync_person_map
    ADD CONSTRAINT _face_sync_person_map_source_person_id_target_user_id_key UNIQUE (source_person_id, target_user_id);


--
-- Name: _face_sync_skipped _face_sync_skipped_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public._face_sync_skipped
    ADD CONSTRAINT _face_sync_skipped_pkey PRIMARY KEY (source_asset_id, target_user_id);


--
-- Name: activity activity_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.activity
    ADD CONSTRAINT activity_pkey PRIMARY KEY (id);


--
-- Name: album_asset_audit album_asset_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.album_asset_audit
    ADD CONSTRAINT album_asset_audit_pkey PRIMARY KEY (id);


--
-- Name: album_asset album_asset_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.album_asset
    ADD CONSTRAINT album_asset_pkey PRIMARY KEY ("albumId", "assetId");


--
-- Name: album_audit album_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.album_audit
    ADD CONSTRAINT album_audit_pkey PRIMARY KEY (id);


--
-- Name: album album_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.album
    ADD CONSTRAINT album_pkey PRIMARY KEY (id);


--
-- Name: album_user_audit album_user_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.album_user_audit
    ADD CONSTRAINT album_user_audit_pkey PRIMARY KEY (id);


--
-- Name: album_user album_user_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.album_user
    ADD CONSTRAINT album_user_pkey PRIMARY KEY ("albumId", "userId");


--
-- Name: api_key api_key_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.api_key
    ADD CONSTRAINT api_key_pkey PRIMARY KEY (id);


--
-- Name: asset_audio asset_audio_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_audio
    ADD CONSTRAINT asset_audio_pkey PRIMARY KEY ("assetId");


--
-- Name: asset_audit asset_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_audit
    ADD CONSTRAINT asset_audit_pkey PRIMARY KEY (id);


--
-- Name: asset_edit asset_edit_assetId_sequence_uq; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_edit
    ADD CONSTRAINT "asset_edit_assetId_sequence_uq" UNIQUE ("assetId", sequence);


--
-- Name: asset_edit_audit asset_edit_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_edit_audit
    ADD CONSTRAINT asset_edit_audit_pkey PRIMARY KEY (id);


--
-- Name: asset_edit asset_edit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_edit
    ADD CONSTRAINT asset_edit_pkey PRIMARY KEY (id);


--
-- Name: asset_exif asset_exif_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_exif
    ADD CONSTRAINT asset_exif_pkey PRIMARY KEY ("assetId");


--
-- Name: asset_face_audit asset_face_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_face_audit
    ADD CONSTRAINT asset_face_audit_pkey PRIMARY KEY (id);


--
-- Name: asset_face asset_face_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_face
    ADD CONSTRAINT asset_face_pkey PRIMARY KEY (id);


--
-- Name: asset_file asset_file_assetId_type_isEdited_uq; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_file
    ADD CONSTRAINT "asset_file_assetId_type_isEdited_uq" UNIQUE ("assetId", type, "isEdited");


--
-- Name: asset_file asset_file_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_file
    ADD CONSTRAINT asset_file_pkey PRIMARY KEY (id);


--
-- Name: asset_job_status asset_job_status_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_job_status
    ADD CONSTRAINT asset_job_status_pkey PRIMARY KEY ("assetId");


--
-- Name: asset_keyframe asset_keyframe_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_keyframe
    ADD CONSTRAINT asset_keyframe_pkey PRIMARY KEY ("assetId");


--
-- Name: asset_metadata_audit asset_metadata_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_metadata_audit
    ADD CONSTRAINT asset_metadata_audit_pkey PRIMARY KEY (id);


--
-- Name: asset_metadata asset_metadata_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_metadata
    ADD CONSTRAINT asset_metadata_pkey PRIMARY KEY ("assetId", key);


--
-- Name: asset_ocr_audit asset_ocr_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_ocr_audit
    ADD CONSTRAINT asset_ocr_audit_pkey PRIMARY KEY (id);


--
-- Name: asset_ocr asset_ocr_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_ocr
    ADD CONSTRAINT asset_ocr_pkey PRIMARY KEY (id);


--
-- Name: asset asset_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset
    ADD CONSTRAINT asset_pkey PRIMARY KEY (id);


--
-- Name: asset_video asset_video_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_video
    ADD CONSTRAINT asset_video_pkey PRIMARY KEY ("assetId");


--
-- Name: cluster_group cluster_group_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cluster_group
    ADD CONSTRAINT cluster_group_pkey PRIMARY KEY (id);


--
-- Name: cluster_group_request cluster_group_request_clusterGroupId_userId_uq; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cluster_group_request
    ADD CONSTRAINT "cluster_group_request_clusterGroupId_userId_uq" UNIQUE ("clusterGroupId", "userId");


--
-- Name: cluster_group_request cluster_group_request_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cluster_group_request
    ADD CONSTRAINT cluster_group_request_pkey PRIMARY KEY (id);


--
-- Name: face_search face_search_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.face_search
    ADD CONSTRAINT face_search_pkey PRIMARY KEY ("faceId");


--
-- Name: geodata_places geodata_places_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.geodata_places
    ADD CONSTRAINT geodata_places_pkey PRIMARY KEY (id) WITH (fillfactor='100');


--
-- Name: integrity_report integrity_report_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.integrity_report
    ADD CONSTRAINT integrity_report_pkey PRIMARY KEY (id);


--
-- Name: integrity_report integrity_report_type_path_uq; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.integrity_report
    ADD CONSTRAINT integrity_report_type_path_uq UNIQUE (type, path);


--
-- Name: kysely_migrations_lock kysely_migrations_lock_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.kysely_migrations_lock
    ADD CONSTRAINT kysely_migrations_lock_pkey PRIMARY KEY (id);


--
-- Name: kysely_migrations kysely_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.kysely_migrations
    ADD CONSTRAINT kysely_migrations_pkey PRIMARY KEY (name);


--
-- Name: library library_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.library
    ADD CONSTRAINT library_pkey PRIMARY KEY (id);


--
-- Name: memory_asset_audit memory_asset_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.memory_asset_audit
    ADD CONSTRAINT memory_asset_audit_pkey PRIMARY KEY (id);


--
-- Name: memory_asset memory_asset_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.memory_asset
    ADD CONSTRAINT memory_asset_pkey PRIMARY KEY ("memoriesId", "assetId");


--
-- Name: memory_audit memory_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.memory_audit
    ADD CONSTRAINT memory_audit_pkey PRIMARY KEY (id);


--
-- Name: memory memory_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.memory
    ADD CONSTRAINT memory_pkey PRIMARY KEY (id);


--
-- Name: migration_overrides migration_overrides_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.migration_overrides
    ADD CONSTRAINT migration_overrides_pkey PRIMARY KEY (name);


--
-- Name: move_history move_history_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.move_history
    ADD CONSTRAINT move_history_pkey PRIMARY KEY (id);


--
-- Name: naturalearth_countries naturalearth_countries_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.naturalearth_countries
    ADD CONSTRAINT naturalearth_countries_pkey PRIMARY KEY (id) WITH (fillfactor='100');


--
-- Name: notification notification_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.notification
    ADD CONSTRAINT notification_pkey PRIMARY KEY (id);


--
-- Name: ocr_search ocr_search_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ocr_search
    ADD CONSTRAINT ocr_search_pkey PRIMARY KEY ("assetId");


--
-- Name: partner_audit partner_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.partner_audit
    ADD CONSTRAINT partner_audit_pkey PRIMARY KEY (id);


--
-- Name: partner partner_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.partner
    ADD CONSTRAINT partner_pkey PRIMARY KEY ("sharedById", "sharedWithId");


--
-- Name: person_audit person_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.person_audit
    ADD CONSTRAINT person_audit_pkey PRIMARY KEY (id);


--
-- Name: person_group_audit person_group_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.person_group_audit
    ADD CONSTRAINT person_group_audit_pkey PRIMARY KEY (id);


--
-- Name: person_group person_group_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.person_group
    ADD CONSTRAINT person_group_pkey PRIMARY KEY (id);


--
-- Name: person person_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.person
    ADD CONSTRAINT person_pkey PRIMARY KEY ("ownerId", "personGroupId");


--
-- Name: plugin_method plugin_method_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.plugin_method
    ADD CONSTRAINT plugin_method_pkey PRIMARY KEY (id);


--
-- Name: plugin_method plugin_method_pluginId_name_uq; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.plugin_method
    ADD CONSTRAINT "plugin_method_pluginId_name_uq" UNIQUE ("pluginId", name);


--
-- Name: plugin plugin_name_uq; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.plugin
    ADD CONSTRAINT plugin_name_uq UNIQUE (name);


--
-- Name: plugin plugin_name_version_uq; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.plugin
    ADD CONSTRAINT plugin_name_version_uq UNIQUE (name, version);


--
-- Name: plugin plugin_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.plugin
    ADD CONSTRAINT plugin_pkey PRIMARY KEY (id);


--
-- Name: session session_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.session
    ADD CONSTRAINT session_pkey PRIMARY KEY (id);


--
-- Name: session_sync_checkpoint session_sync_checkpoint_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.session_sync_checkpoint
    ADD CONSTRAINT session_sync_checkpoint_pkey PRIMARY KEY ("sessionId", type);


--
-- Name: shared_link_asset shared_link_asset_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.shared_link_asset
    ADD CONSTRAINT shared_link_asset_pkey PRIMARY KEY ("assetId", "sharedLinkId");


--
-- Name: shared_link shared_link_key_uq; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.shared_link
    ADD CONSTRAINT shared_link_key_uq UNIQUE (key);


--
-- Name: shared_link shared_link_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.shared_link
    ADD CONSTRAINT shared_link_pkey PRIMARY KEY (id);


--
-- Name: shared_link shared_link_slug_uq; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.shared_link
    ADD CONSTRAINT shared_link_slug_uq UNIQUE (slug);


--
-- Name: smart_search smart_search_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.smart_search
    ADD CONSTRAINT smart_search_pkey PRIMARY KEY ("assetId");


--
-- Name: stack_audit stack_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.stack_audit
    ADD CONSTRAINT stack_audit_pkey PRIMARY KEY (id);


--
-- Name: stack stack_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.stack
    ADD CONSTRAINT stack_pkey PRIMARY KEY (id);


--
-- Name: stack stack_primaryAssetId_uq; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.stack
    ADD CONSTRAINT "stack_primaryAssetId_uq" UNIQUE ("primaryAssetId");


--
-- Name: system_metadata system_metadata_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.system_metadata
    ADD CONSTRAINT system_metadata_pkey PRIMARY KEY (key);


--
-- Name: tag_asset tag_asset_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tag_asset
    ADD CONSTRAINT tag_asset_pkey PRIMARY KEY ("assetId", "tagId");


--
-- Name: tag_closure tag_closure_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tag_closure
    ADD CONSTRAINT tag_closure_pkey PRIMARY KEY (id_ancestor, id_descendant);


--
-- Name: tag tag_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tag
    ADD CONSTRAINT tag_pkey PRIMARY KEY (id);


--
-- Name: tag tag_userId_value_uq; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tag
    ADD CONSTRAINT "tag_userId_value_uq" UNIQUE ("userId", value);


--
-- Name: user_audit user_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.user_audit
    ADD CONSTRAINT user_audit_pkey PRIMARY KEY (id);


--
-- Name: user user_email_uq; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public."user"
    ADD CONSTRAINT user_email_uq UNIQUE (email);


--
-- Name: user_metadata_audit user_metadata_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.user_metadata_audit
    ADD CONSTRAINT user_metadata_audit_pkey PRIMARY KEY (id);


--
-- Name: user_metadata user_metadata_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.user_metadata
    ADD CONSTRAINT user_metadata_pkey PRIMARY KEY ("userId", key);


--
-- Name: user user_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public."user"
    ADD CONSTRAINT user_pkey PRIMARY KEY (id);


--
-- Name: user user_storageLabel_uq; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public."user"
    ADD CONSTRAINT "user_storageLabel_uq" UNIQUE ("storageLabel");


--
-- Name: version_history version_history_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.version_history
    ADD CONSTRAINT version_history_pkey PRIMARY KEY (id);


--
-- Name: video_stream_segment video_stream_segment_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.video_stream_segment
    ADD CONSTRAINT video_stream_segment_pkey PRIMARY KEY ("variantId", index);


--
-- Name: video_stream_session video_stream_session_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.video_stream_session
    ADD CONSTRAINT video_stream_session_pkey PRIMARY KEY (id);


--
-- Name: video_stream_variant video_stream_variant_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.video_stream_variant
    ADD CONSTRAINT video_stream_variant_pkey PRIMARY KEY (id);


--
-- Name: workflow_log workflow_log_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.workflow_log
    ADD CONSTRAINT workflow_log_pkey PRIMARY KEY (id);


--
-- Name: workflow workflow_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.workflow
    ADD CONSTRAINT workflow_pkey PRIMARY KEY (id);


--
-- Name: workflow_step workflow_step_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.workflow_step
    ADD CONSTRAINT workflow_step_pkey PRIMARY KEY (id);


--
-- Name: IDX_asset_exif_gist_earthcoord; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "IDX_asset_exif_gist_earthcoord" ON public.asset_exif USING gist (public.ll_to_earth_public(latitude, longitude));


--
-- Name: IDX_geodata_gist_earthcoord; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "IDX_geodata_gist_earthcoord" ON public.geodata_places USING gist (public.ll_to_earth_public(latitude, longitude));


--
-- Name: IDX_user_metadata_audit_deleted_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "IDX_user_metadata_audit_deleted_at" ON public.user_metadata_audit USING btree ("deletedAt");


--
-- Name: IDX_user_metadata_audit_key; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "IDX_user_metadata_audit_key" ON public.user_metadata_audit USING btree (key);


--
-- Name: IDX_user_metadata_audit_user_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "IDX_user_metadata_audit_user_id" ON public.user_metadata_audit USING btree ("userId");


--
-- Name: IDX_user_metadata_update_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "IDX_user_metadata_update_id" ON public.user_metadata USING btree ("updateId");


--
-- Name: IDX_user_metadata_updated_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "IDX_user_metadata_updated_at" ON public.user_metadata USING btree ("updatedAt");


--
-- Name: UQ_assets_owner_checksum; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX "UQ_assets_owner_checksum" ON public.asset USING btree ("ownerId", checksum) WHERE ("libraryId" IS NULL);


--
-- Name: activity_albumId_assetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "activity_albumId_assetId_idx" ON public.activity USING btree ("albumId", "assetId");


--
-- Name: activity_albumId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "activity_albumId_idx" ON public.activity USING btree ("albumId");


--
-- Name: activity_assetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "activity_assetId_idx" ON public.activity USING btree ("assetId");


--
-- Name: activity_like_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX activity_like_idx ON public.activity USING btree ("assetId", "userId", "albumId") WHERE ("isLiked" = true);


--
-- Name: activity_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "activity_updateId_idx" ON public.activity USING btree ("updateId");


--
-- Name: activity_userId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "activity_userId_idx" ON public.activity USING btree ("userId");


--
-- Name: album_albumThumbnailAssetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "album_albumThumbnailAssetId_idx" ON public.album USING btree ("albumThumbnailAssetId");


--
-- Name: album_asset_albumId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "album_asset_albumId_idx" ON public.album_asset USING btree ("albumId");


--
-- Name: album_asset_assetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "album_asset_assetId_idx" ON public.album_asset USING btree ("assetId");


--
-- Name: album_asset_audit_albumId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "album_asset_audit_albumId_idx" ON public.album_asset_audit USING btree ("albumId");


--
-- Name: album_asset_audit_assetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "album_asset_audit_assetId_idx" ON public.album_asset_audit USING btree ("assetId");


--
-- Name: album_asset_audit_deletedAt_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "album_asset_audit_deletedAt_idx" ON public.album_asset_audit USING btree ("deletedAt");


--
-- Name: album_asset_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "album_asset_updateId_idx" ON public.album_asset USING btree ("updateId");


--
-- Name: album_audit_albumId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "album_audit_albumId_idx" ON public.album_audit USING btree ("albumId");


--
-- Name: album_audit_deletedAt_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "album_audit_deletedAt_idx" ON public.album_audit USING btree ("deletedAt");


--
-- Name: album_audit_userId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "album_audit_userId_idx" ON public.album_audit USING btree ("userId");


--
-- Name: album_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "album_updateId_idx" ON public.album USING btree ("updateId");


--
-- Name: album_user_albumId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "album_user_albumId_idx" ON public.album_user USING btree ("albumId");


--
-- Name: album_user_audit_albumId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "album_user_audit_albumId_idx" ON public.album_user_audit USING btree ("albumId");


--
-- Name: album_user_audit_deletedAt_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "album_user_audit_deletedAt_idx" ON public.album_user_audit USING btree ("deletedAt");


--
-- Name: album_user_audit_userId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "album_user_audit_userId_idx" ON public.album_user_audit USING btree ("userId");


--
-- Name: album_user_createId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "album_user_createId_idx" ON public.album_user USING btree ("createId");


--
-- Name: album_user_unique_owner; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX album_user_unique_owner ON public.album_user USING btree ("albumId") WHERE (role = 'owner'::public.album_user_role_enum);


--
-- Name: album_user_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "album_user_updateId_idx" ON public.album_user USING btree ("updateId");


--
-- Name: album_user_userId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "album_user_userId_idx" ON public.album_user USING btree ("userId");


--
-- Name: api_key_key_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX api_key_key_idx ON public.api_key USING btree (key);


--
-- Name: api_key_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "api_key_updateId_idx" ON public.api_key USING btree ("updateId");


--
-- Name: api_key_userId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "api_key_userId_idx" ON public.api_key USING btree ("userId");


--
-- Name: asset_audit_assetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_audit_assetId_idx" ON public.asset_audit USING btree ("assetId");


--
-- Name: asset_audit_deletedAt_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_audit_deletedAt_idx" ON public.asset_audit USING btree ("deletedAt");


--
-- Name: asset_audit_ownerId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_audit_ownerId_idx" ON public.asset_audit USING btree ("ownerId");


--
-- Name: asset_checksum_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX asset_checksum_idx ON public.asset USING btree (checksum);


--
-- Name: asset_createdAt_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_createdAt_idx" ON public.asset USING btree ("createdAt");


--
-- Name: asset_duplicateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_duplicateId_idx" ON public.asset USING btree ("duplicateId");


--
-- Name: asset_edit_assetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_edit_assetId_idx" ON public.asset_edit USING btree ("assetId");


--
-- Name: asset_edit_audit_assetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_edit_audit_assetId_idx" ON public.asset_edit_audit USING btree ("assetId");


--
-- Name: asset_edit_audit_deletedAt_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_edit_audit_deletedAt_idx" ON public.asset_edit_audit USING btree ("deletedAt");


--
-- Name: asset_edit_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_edit_updateId_idx" ON public.asset_edit USING btree ("updateId");


--
-- Name: asset_exif_autoStackId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_exif_autoStackId_idx" ON public.asset_exif USING btree ("autoStackId");


--
-- Name: asset_exif_city_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX asset_exif_city_idx ON public.asset_exif USING btree (city);


--
-- Name: asset_exif_livePhotoCID_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_exif_livePhotoCID_idx" ON public.asset_exif USING btree ("livePhotoCID");


--
-- Name: asset_exif_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_exif_updateId_idx" ON public.asset_exif USING btree ("updateId");


--
-- Name: asset_face_assetId_personGroupId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_face_assetId_personGroupId_idx" ON public.asset_face USING btree ("assetId", "personGroupId");


--
-- Name: asset_face_audit_assetFaceId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_face_audit_assetFaceId_idx" ON public.asset_face_audit USING btree ("assetFaceId");


--
-- Name: asset_face_audit_assetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_face_audit_assetId_idx" ON public.asset_face_audit USING btree ("assetId");


--
-- Name: asset_face_audit_deletedAt_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_face_audit_deletedAt_idx" ON public.asset_face_audit USING btree ("deletedAt");


--
-- Name: asset_face_personGroupId_assetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_face_personGroupId_assetId_idx" ON public.asset_face USING btree ("personGroupId", "assetId");


--
-- Name: asset_face_personGroupId_assetId_notDeleted_isVisible_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_face_personGroupId_assetId_notDeleted_isVisible_idx" ON public.asset_face USING btree ("personGroupId", "assetId") WHERE (("deletedAt" IS NULL) AND ("isVisible" IS TRUE));


--
-- Name: asset_fileCreatedAt_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_fileCreatedAt_idx" ON public.asset USING btree ("fileCreatedAt");


--
-- Name: asset_file_assetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_file_assetId_idx" ON public.asset_file USING btree ("assetId");


--
-- Name: asset_file_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_file_updateId_idx" ON public.asset_file USING btree ("updateId");


--
-- Name: asset_id_stackId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_id_stackId_idx" ON public.asset USING btree (id, "stackId");


--
-- Name: asset_id_timeline_notDeleted_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_id_timeline_notDeleted_idx" ON public.asset USING btree (id) WHERE ((visibility = 'timeline'::public.asset_visibility_enum) AND ("deletedAt" IS NULL));


--
-- Name: asset_libraryId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_libraryId_idx" ON public.asset USING btree ("libraryId");


--
-- Name: asset_livePhotoVideoId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_livePhotoVideoId_idx" ON public.asset USING btree ("livePhotoVideoId");


--
-- Name: asset_localDateTime_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_localDateTime_idx" ON public.asset USING btree (((("localDateTime" AT TIME ZONE 'UTC'::text))::date));


--
-- Name: asset_localDateTime_month_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_localDateTime_month_idx" ON public.asset USING btree ((date_trunc('MONTH'::text, ("localDateTime" AT TIME ZONE 'UTC'::text)) AT TIME ZONE 'UTC'::text));


--
-- Name: asset_metadata_audit_assetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_metadata_audit_assetId_idx" ON public.asset_metadata_audit USING btree ("assetId");


--
-- Name: asset_metadata_audit_deletedAt_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_metadata_audit_deletedAt_idx" ON public.asset_metadata_audit USING btree ("deletedAt");


--
-- Name: asset_metadata_audit_key_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX asset_metadata_audit_key_idx ON public.asset_metadata_audit USING btree (key);


--
-- Name: asset_metadata_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_metadata_updateId_idx" ON public.asset_metadata USING btree ("updateId");


--
-- Name: asset_metadata_updatedAt_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_metadata_updatedAt_idx" ON public.asset_metadata USING btree ("updatedAt");


--
-- Name: asset_ocr_assetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_ocr_assetId_idx" ON public.asset_ocr USING btree ("assetId");


--
-- Name: asset_ocr_audit_assetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_ocr_audit_assetId_idx" ON public.asset_ocr_audit USING btree ("assetId");


--
-- Name: asset_ocr_audit_deletedAt_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_ocr_audit_deletedAt_idx" ON public.asset_ocr_audit USING btree ("deletedAt");


--
-- Name: asset_ocr_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_ocr_updateId_idx" ON public.asset_ocr USING btree ("updateId");


--
-- Name: asset_originalFileName_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_originalFileName_idx" ON public.asset USING btree ("originalFileName");


--
-- Name: asset_originalFilename_trigram_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_originalFilename_trigram_idx" ON public.asset USING gin (public.f_unaccent(("originalFileName")::text) public.gin_trgm_ops);


--
-- Name: asset_originalPath_libraryId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_originalPath_libraryId_idx" ON public.asset USING btree ("originalPath", "libraryId");


--
-- Name: asset_ownerId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_ownerId_idx" ON public.asset USING btree ("ownerId");


--
-- Name: asset_ownerId_libraryId_checksum_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX "asset_ownerId_libraryId_checksum_idx" ON public.asset USING btree ("ownerId", "libraryId", checksum) WHERE ("libraryId" IS NOT NULL);


--
-- Name: asset_stackId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_stackId_idx" ON public.asset USING btree ("stackId");


--
-- Name: asset_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "asset_updateId_idx" ON public.asset USING btree ("updateId");


--
-- Name: clip_index; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX clip_index ON public.smart_search USING vchordrq (embedding public.vector_cosine_ops) WITH (options='
        residual_quantization = false
        [build.internal]
        lists = [1]
        spherical_centroids = true
        build_threads = 4
        sampling_factor = 1024
        ');


--
-- Name: cluster_group_request_clusterGroupId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "cluster_group_request_clusterGroupId_idx" ON public.cluster_group_request USING btree ("clusterGroupId");


--
-- Name: cluster_group_request_userId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "cluster_group_request_userId_idx" ON public.cluster_group_request USING btree ("userId");


--
-- Name: cluster_group_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "cluster_group_updateId_idx" ON public.cluster_group USING btree ("updateId");


--
-- Name: face_index; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX face_index ON public.face_search USING vchordrq (embedding public.vector_cosine_ops) WITH (options='
        residual_quantization = false
        [build.internal]
        lists = [1]
        spherical_centroids = true
        build_threads = 4
        sampling_factor = 1024
        ');


--
-- Name: idx_face_sync_person_map_target; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_face_sync_person_map_target ON public._face_sync_person_map USING btree (target_person_id);


--
-- Name: idx_geodata_places_admin1_name; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_geodata_places_admin1_name ON public.geodata_places USING gin (public.f_unaccent(("admin1Name")::text) public.gin_trgm_ops);


--
-- Name: idx_geodata_places_admin2_name; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_geodata_places_admin2_name ON public.geodata_places USING gin (public.f_unaccent(("admin2Name")::text) public.gin_trgm_ops);


--
-- Name: idx_geodata_places_alternate_names; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_geodata_places_alternate_names ON public.geodata_places USING gin (public.f_unaccent(("alternateNames")::text) public.gin_trgm_ops);


--
-- Name: idx_geodata_places_name; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_geodata_places_name ON public.geodata_places USING gin (public.f_unaccent((name)::text) public.gin_trgm_ops);


--
-- Name: idx_ocr_search_text; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_ocr_search_text ON public.ocr_search USING gin (public.f_unaccent(text) public.gin_trgm_ops);


--
-- Name: idx_person_name_trigram; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_person_name_trigram ON public.person USING gin (public.f_unaccent((name)::text) public.gin_trgm_ops);


--
-- Name: integrity_report_assetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "integrity_report_assetId_idx" ON public.integrity_report USING btree ("assetId");


--
-- Name: integrity_report_fileAssetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "integrity_report_fileAssetId_idx" ON public.integrity_report USING btree ("fileAssetId");


--
-- Name: library_ownerId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "library_ownerId_idx" ON public.library USING btree ("ownerId");


--
-- Name: library_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "library_updateId_idx" ON public.library USING btree ("updateId");


--
-- Name: memory_asset_assetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "memory_asset_assetId_idx" ON public.memory_asset USING btree ("assetId");


--
-- Name: memory_asset_audit_assetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "memory_asset_audit_assetId_idx" ON public.memory_asset_audit USING btree ("assetId");


--
-- Name: memory_asset_audit_deletedAt_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "memory_asset_audit_deletedAt_idx" ON public.memory_asset_audit USING btree ("deletedAt");


--
-- Name: memory_asset_audit_memoryId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "memory_asset_audit_memoryId_idx" ON public.memory_asset_audit USING btree ("memoryId");


--
-- Name: memory_asset_memoriesId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "memory_asset_memoriesId_idx" ON public.memory_asset USING btree ("memoriesId");


--
-- Name: memory_asset_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "memory_asset_updateId_idx" ON public.memory_asset USING btree ("updateId");


--
-- Name: memory_audit_deletedAt_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "memory_audit_deletedAt_idx" ON public.memory_audit USING btree ("deletedAt");


--
-- Name: memory_audit_memoryId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "memory_audit_memoryId_idx" ON public.memory_audit USING btree ("memoryId");


--
-- Name: memory_audit_userId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "memory_audit_userId_idx" ON public.memory_audit USING btree ("userId");


--
-- Name: memory_ownerId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "memory_ownerId_idx" ON public.memory USING btree ("ownerId");


--
-- Name: memory_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "memory_updateId_idx" ON public.memory USING btree ("updateId");


--
-- Name: notification_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "notification_updateId_idx" ON public.notification USING btree ("updateId");


--
-- Name: notification_userId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "notification_userId_idx" ON public.notification USING btree ("userId");


--
-- Name: partner_audit_deletedAt_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "partner_audit_deletedAt_idx" ON public.partner_audit USING btree ("deletedAt");


--
-- Name: partner_audit_sharedById_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "partner_audit_sharedById_idx" ON public.partner_audit USING btree ("sharedById");


--
-- Name: partner_audit_sharedWithId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "partner_audit_sharedWithId_idx" ON public.partner_audit USING btree ("sharedWithId");


--
-- Name: partner_createId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "partner_createId_idx" ON public.partner USING btree ("createId");


--
-- Name: partner_sharedWithId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "partner_sharedWithId_idx" ON public.partner USING btree ("sharedWithId");


--
-- Name: partner_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "partner_updateId_idx" ON public.partner USING btree ("updateId");


--
-- Name: person_audit_deletedAt_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "person_audit_deletedAt_idx" ON public.person_audit USING btree ("deletedAt");


--
-- Name: person_audit_ownerId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "person_audit_ownerId_idx" ON public.person_audit USING btree ("ownerId");


--
-- Name: person_audit_personGroupId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "person_audit_personGroupId_idx" ON public.person_audit USING btree ("personGroupId");


--
-- Name: person_faceAssetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "person_faceAssetId_idx" ON public.person USING btree ("faceAssetId");


--
-- Name: person_group_audit_clusterGroupId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "person_group_audit_clusterGroupId_idx" ON public.person_group_audit USING btree ("clusterGroupId");


--
-- Name: person_group_audit_deletedAt_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "person_group_audit_deletedAt_idx" ON public.person_group_audit USING btree ("deletedAt");


--
-- Name: person_group_audit_personGroupId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "person_group_audit_personGroupId_idx" ON public.person_group_audit USING btree ("personGroupId");


--
-- Name: person_group_clusterGroupId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "person_group_clusterGroupId_idx" ON public.person_group USING btree ("clusterGroupId");


--
-- Name: person_group_createId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "person_group_createId_idx" ON public.person_group USING btree ("createId");


--
-- Name: person_group_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "person_group_updateId_idx" ON public.person_group USING btree ("updateId");


--
-- Name: person_personGroupId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "person_personGroupId_idx" ON public.person USING btree ("personGroupId");


--
-- Name: person_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "person_updateId_idx" ON public.person USING btree ("updateId");


--
-- Name: plugin_method_pluginId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "plugin_method_pluginId_idx" ON public.plugin_method USING btree ("pluginId");


--
-- Name: plugin_name_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX plugin_name_idx ON public.plugin USING btree (name);


--
-- Name: session_oauthSid_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "session_oauthSid_idx" ON public.session USING btree ("oauthSid");


--
-- Name: session_parentId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "session_parentId_idx" ON public.session USING btree ("parentId");


--
-- Name: session_sync_checkpoint_sessionId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "session_sync_checkpoint_sessionId_idx" ON public.session_sync_checkpoint USING btree ("sessionId");


--
-- Name: session_sync_checkpoint_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "session_sync_checkpoint_updateId_idx" ON public.session_sync_checkpoint USING btree ("updateId");


--
-- Name: session_token_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX session_token_idx ON public.session USING btree (token);


--
-- Name: session_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "session_updateId_idx" ON public.session USING btree ("updateId");


--
-- Name: session_userId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "session_userId_idx" ON public.session USING btree ("userId");


--
-- Name: shared_link_albumId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "shared_link_albumId_idx" ON public.shared_link USING btree ("albumId");


--
-- Name: shared_link_asset_assetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "shared_link_asset_assetId_idx" ON public.shared_link_asset USING btree ("assetId");


--
-- Name: shared_link_asset_sharedLinkId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "shared_link_asset_sharedLinkId_idx" ON public.shared_link_asset USING btree ("sharedLinkId");


--
-- Name: shared_link_key_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX shared_link_key_idx ON public.shared_link USING btree (key);


--
-- Name: shared_link_userId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "shared_link_userId_idx" ON public.shared_link USING btree ("userId");


--
-- Name: stack_audit_deletedAt_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "stack_audit_deletedAt_idx" ON public.stack_audit USING btree ("deletedAt");


--
-- Name: stack_ownerId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "stack_ownerId_idx" ON public.stack USING btree ("ownerId");


--
-- Name: stack_primaryAssetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "stack_primaryAssetId_idx" ON public.stack USING btree ("primaryAssetId");


--
-- Name: tag_asset_assetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "tag_asset_assetId_idx" ON public.tag_asset USING btree ("assetId");


--
-- Name: tag_asset_assetId_tagId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "tag_asset_assetId_tagId_idx" ON public.tag_asset USING btree ("assetId", "tagId");


--
-- Name: tag_asset_tagId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "tag_asset_tagId_idx" ON public.tag_asset USING btree ("tagId");


--
-- Name: tag_closure_id_ancestor_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX tag_closure_id_ancestor_idx ON public.tag_closure USING btree (id_ancestor);


--
-- Name: tag_closure_id_descendant_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX tag_closure_id_descendant_idx ON public.tag_closure USING btree (id_descendant);


--
-- Name: tag_parentId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "tag_parentId_idx" ON public.tag USING btree ("parentId");


--
-- Name: tag_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "tag_updateId_idx" ON public.tag USING btree ("updateId");


--
-- Name: user_audit_deletedAt_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "user_audit_deletedAt_idx" ON public.user_audit USING btree ("deletedAt");


--
-- Name: user_clusterGroupId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "user_clusterGroupId_idx" ON public."user" USING btree ("clusterGroupId");


--
-- Name: user_updateId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "user_updateId_idx" ON public."user" USING btree ("updateId");


--
-- Name: user_updatedAt_id_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "user_updatedAt_id_idx" ON public."user" USING btree ("updatedAt", id);


--
-- Name: video_stream_session_assetId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "video_stream_session_assetId_idx" ON public.video_stream_session USING btree ("assetId");


--
-- Name: video_stream_session_expiresAt_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "video_stream_session_expiresAt_idx" ON public.video_stream_session USING btree ("expiresAt");


--
-- Name: video_stream_variant_sessionId_bitrate_resolution_codec_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX "video_stream_variant_sessionId_bitrate_resolution_codec_idx" ON public.video_stream_variant USING btree ("sessionId", bitrate, resolution, codec);


--
-- Name: workflow_log_workflowId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "workflow_log_workflowId_idx" ON public.workflow_log USING btree ("workflowId");


--
-- Name: workflow_log_workflowStepId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "workflow_log_workflowStepId_idx" ON public.workflow_log USING btree ("workflowStepId");


--
-- Name: workflow_ownerId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "workflow_ownerId_idx" ON public.workflow USING btree ("ownerId");


--
-- Name: workflow_step_pluginMethodId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "workflow_step_pluginMethodId_idx" ON public.workflow_step USING btree ("pluginMethodId");


--
-- Name: workflow_step_workflowId_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "workflow_step_workflowId_idx" ON public.workflow_step USING btree ("workflowId");


--
-- Name: activity activity_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "activity_updatedAt" BEFORE UPDATE ON public.activity FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: album_asset album_asset_delete_audit; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER album_asset_delete_audit AFTER DELETE ON public.album_asset REFERENCING OLD TABLE AS old FOR EACH STATEMENT WHEN ((pg_trigger_depth() <= 1)) EXECUTE FUNCTION public.album_asset_delete_audit();


--
-- Name: album_asset album_asset_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "album_asset_updatedAt" BEFORE UPDATE ON public.album_asset FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: album album_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "album_updatedAt" BEFORE UPDATE ON public.album FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: album_user album_user_after_insert; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER album_user_after_insert AFTER INSERT ON public.album_user REFERENCING NEW TABLE AS inserted_rows FOR EACH STATEMENT EXECUTE FUNCTION public.album_user_after_insert();


--
-- Name: album_user album_user_delete; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER album_user_delete AFTER DELETE ON public.album_user REFERENCING OLD TABLE AS old FOR EACH ROW EXECUTE FUNCTION public.album_user_delete();


--
-- Name: album_user album_user_delete_audit; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER album_user_delete_audit AFTER DELETE ON public.album_user REFERENCING OLD TABLE AS old FOR EACH STATEMENT WHEN ((pg_trigger_depth() <= 1)) EXECUTE FUNCTION public.album_user_delete_audit();


--
-- Name: album_user album_user_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "album_user_updatedAt" BEFORE UPDATE ON public.album_user FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: api_key api_key_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "api_key_updatedAt" BEFORE UPDATE ON public.api_key FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: asset asset_delete_audit; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER asset_delete_audit AFTER DELETE ON public.asset REFERENCING OLD TABLE AS old FOR EACH STATEMENT WHEN ((pg_trigger_depth() = 0)) EXECUTE FUNCTION public.asset_delete_audit();


--
-- Name: asset_edit asset_edit_audit; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER asset_edit_audit AFTER DELETE ON public.asset_edit REFERENCING OLD TABLE AS old FOR EACH STATEMENT WHEN ((pg_trigger_depth() = 0)) EXECUTE FUNCTION public.asset_edit_audit();


--
-- Name: asset_edit asset_edit_delete; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER asset_edit_delete AFTER DELETE ON public.asset_edit REFERENCING OLD TABLE AS deleted_edit FOR EACH STATEMENT WHEN ((pg_trigger_depth() = 0)) EXECUTE FUNCTION public.asset_edit_delete();


--
-- Name: asset_edit asset_edit_insert; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER asset_edit_insert AFTER INSERT ON public.asset_edit REFERENCING NEW TABLE AS inserted_edit FOR EACH STATEMENT EXECUTE FUNCTION public.asset_edit_insert();


--
-- Name: asset_edit asset_edit_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "asset_edit_updatedAt" BEFORE UPDATE ON public.asset_edit FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: asset_exif asset_exif_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "asset_exif_updatedAt" BEFORE UPDATE ON public.asset_exif FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: asset_face asset_face_audit; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER asset_face_audit AFTER DELETE ON public.asset_face REFERENCING OLD TABLE AS old FOR EACH STATEMENT WHEN ((pg_trigger_depth() = 0)) EXECUTE FUNCTION public.asset_face_audit();


--
-- Name: asset_face asset_face_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "asset_face_updatedAt" BEFORE UPDATE ON public.asset_face FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: asset_file asset_file_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "asset_file_updatedAt" BEFORE UPDATE ON public.asset_file FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: asset_metadata asset_metadata_audit; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER asset_metadata_audit AFTER DELETE ON public.asset_metadata REFERENCING OLD TABLE AS old FOR EACH STATEMENT WHEN ((pg_trigger_depth() = 0)) EXECUTE FUNCTION public.asset_metadata_audit();


--
-- Name: asset_metadata asset_metadata_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER asset_metadata_updated_at BEFORE UPDATE ON public.asset_metadata FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: asset_ocr asset_ocr_delete_audit; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER asset_ocr_delete_audit AFTER DELETE ON public.asset_ocr REFERENCING OLD TABLE AS old FOR EACH STATEMENT WHEN ((pg_trigger_depth() = 0)) EXECUTE FUNCTION public.asset_ocr_delete_audit();


--
-- Name: asset_ocr asset_ocr_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "asset_ocr_updatedAt" BEFORE UPDATE ON public.asset_ocr FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: asset asset_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "asset_updatedAt" BEFORE UPDATE ON public.asset FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: cluster_group cluster_group_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "cluster_group_updatedAt" BEFORE UPDATE ON public.cluster_group FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: library library_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "library_updatedAt" BEFORE UPDATE ON public.library FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: memory_asset memory_asset_delete_audit; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER memory_asset_delete_audit AFTER DELETE ON public.memory_asset REFERENCING OLD TABLE AS old FOR EACH STATEMENT WHEN ((pg_trigger_depth() <= 1)) EXECUTE FUNCTION public.memory_asset_delete_audit();


--
-- Name: memory_asset memory_asset_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "memory_asset_updatedAt" BEFORE UPDATE ON public.memory_asset FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: memory memory_delete_audit; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER memory_delete_audit AFTER DELETE ON public.memory REFERENCING OLD TABLE AS old FOR EACH STATEMENT WHEN ((pg_trigger_depth() = 0)) EXECUTE FUNCTION public.memory_delete_audit();


--
-- Name: memory memory_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "memory_updatedAt" BEFORE UPDATE ON public.memory FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: notification notification_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "notification_updatedAt" BEFORE UPDATE ON public.notification FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: partner partner_delete_audit; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER partner_delete_audit AFTER DELETE ON public.partner REFERENCING OLD TABLE AS old FOR EACH STATEMENT WHEN ((pg_trigger_depth() = 0)) EXECUTE FUNCTION public.partner_delete_audit();


--
-- Name: partner partner_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "partner_updatedAt" BEFORE UPDATE ON public.partner FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: person person_delete_audit; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER person_delete_audit AFTER DELETE ON public.person REFERENCING OLD TABLE AS old FOR EACH STATEMENT WHEN ((pg_trigger_depth() <= 1)) EXECUTE FUNCTION public.person_delete_audit();


--
-- Name: person_group person_group_delete_audit; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER person_group_delete_audit AFTER DELETE ON public.person_group REFERENCING OLD TABLE AS old FOR EACH STATEMENT WHEN ((pg_trigger_depth() = 0)) EXECUTE FUNCTION public.person_group_delete_audit();


--
-- Name: person_group person_group_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "person_group_updatedAt" BEFORE UPDATE ON public.person_group FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: person person_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "person_updatedAt" BEFORE UPDATE ON public.person FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: session_sync_checkpoint session_sync_checkpoint_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "session_sync_checkpoint_updatedAt" BEFORE UPDATE ON public.session_sync_checkpoint FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: session session_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "session_updatedAt" BEFORE UPDATE ON public.session FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: stack stack_delete_audit; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER stack_delete_audit AFTER DELETE ON public.stack REFERENCING OLD TABLE AS old FOR EACH STATEMENT WHEN ((pg_trigger_depth() = 0)) EXECUTE FUNCTION public.stack_delete_audit();


--
-- Name: stack stack_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "stack_updatedAt" BEFORE UPDATE ON public.stack FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: tag tag_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "tag_updatedAt" BEFORE UPDATE ON public.tag FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: user user_delete_audit; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER user_delete_audit AFTER DELETE ON public."user" REFERENCING OLD TABLE AS old FOR EACH STATEMENT WHEN ((pg_trigger_depth() = 0)) EXECUTE FUNCTION public.user_delete_audit();


--
-- Name: user_metadata user_metadata_audit; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER user_metadata_audit AFTER DELETE ON public.user_metadata REFERENCING OLD TABLE AS old FOR EACH STATEMENT WHEN ((pg_trigger_depth() = 0)) EXECUTE FUNCTION public.user_metadata_audit();


--
-- Name: user_metadata user_metadata_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER user_metadata_updated_at BEFORE UPDATE ON public.user_metadata FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: user user_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "user_updatedAt" BEFORE UPDATE ON public."user" FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: workflow workflow_updatedAt; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "workflow_updatedAt" BEFORE UPDATE ON public.workflow FOR EACH ROW EXECUTE FUNCTION public.updated_at();


--
-- Name: activity activity_albumId_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.activity
    ADD CONSTRAINT "activity_albumId_assetId_fkey" FOREIGN KEY ("albumId", "assetId") REFERENCES public.album_asset("albumId", "assetId") ON DELETE CASCADE;


--
-- Name: activity activity_albumId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.activity
    ADD CONSTRAINT "activity_albumId_fkey" FOREIGN KEY ("albumId") REFERENCES public.album(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: activity activity_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.activity
    ADD CONSTRAINT "activity_assetId_fkey" FOREIGN KEY ("assetId") REFERENCES public.asset(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: activity activity_userId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.activity
    ADD CONSTRAINT "activity_userId_fkey" FOREIGN KEY ("userId") REFERENCES public."user"(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: album album_albumThumbnailAssetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.album
    ADD CONSTRAINT "album_albumThumbnailAssetId_fkey" FOREIGN KEY ("albumThumbnailAssetId") REFERENCES public.asset(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: album_asset album_asset_albumId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.album_asset
    ADD CONSTRAINT "album_asset_albumId_fkey" FOREIGN KEY ("albumId") REFERENCES public.album(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: album_asset album_asset_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.album_asset
    ADD CONSTRAINT "album_asset_assetId_fkey" FOREIGN KEY ("assetId") REFERENCES public.asset(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: album_asset_audit album_asset_audit_albumId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.album_asset_audit
    ADD CONSTRAINT "album_asset_audit_albumId_fkey" FOREIGN KEY ("albumId") REFERENCES public.album(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: album_user album_user_albumId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.album_user
    ADD CONSTRAINT "album_user_albumId_fkey" FOREIGN KEY ("albumId") REFERENCES public.album(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: album_user album_user_userId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.album_user
    ADD CONSTRAINT "album_user_userId_fkey" FOREIGN KEY ("userId") REFERENCES public."user"(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: api_key api_key_userId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.api_key
    ADD CONSTRAINT "api_key_userId_fkey" FOREIGN KEY ("userId") REFERENCES public."user"(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: asset_audio asset_audio_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_audio
    ADD CONSTRAINT "asset_audio_assetId_fkey" FOREIGN KEY ("assetId") REFERENCES public.asset(id) ON DELETE CASCADE;


--
-- Name: asset_edit asset_edit_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_edit
    ADD CONSTRAINT "asset_edit_assetId_fkey" FOREIGN KEY ("assetId") REFERENCES public.asset(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: asset_exif asset_exif_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_exif
    ADD CONSTRAINT "asset_exif_assetId_fkey" FOREIGN KEY ("assetId") REFERENCES public.asset(id) ON DELETE CASCADE;


--
-- Name: asset_face asset_face_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_face
    ADD CONSTRAINT "asset_face_assetId_fkey" FOREIGN KEY ("assetId") REFERENCES public.asset(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: asset_face asset_face_personGroupId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_face
    ADD CONSTRAINT "asset_face_personGroupId_fkey" FOREIGN KEY ("personGroupId") REFERENCES public.person_group(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: asset_file asset_file_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_file
    ADD CONSTRAINT "asset_file_assetId_fkey" FOREIGN KEY ("assetId") REFERENCES public.asset(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: asset_job_status asset_job_status_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_job_status
    ADD CONSTRAINT "asset_job_status_assetId_fkey" FOREIGN KEY ("assetId") REFERENCES public.asset(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: asset_keyframe asset_keyframe_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_keyframe
    ADD CONSTRAINT "asset_keyframe_assetId_fkey" FOREIGN KEY ("assetId") REFERENCES public.asset(id) ON DELETE CASCADE;


--
-- Name: asset asset_libraryId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset
    ADD CONSTRAINT "asset_libraryId_fkey" FOREIGN KEY ("libraryId") REFERENCES public.library(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: asset asset_livePhotoVideoId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset
    ADD CONSTRAINT "asset_livePhotoVideoId_fkey" FOREIGN KEY ("livePhotoVideoId") REFERENCES public.asset(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: asset_metadata asset_metadata_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_metadata
    ADD CONSTRAINT "asset_metadata_assetId_fkey" FOREIGN KEY ("assetId") REFERENCES public.asset(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: asset_ocr asset_ocr_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_ocr
    ADD CONSTRAINT "asset_ocr_assetId_fkey" FOREIGN KEY ("assetId") REFERENCES public.asset(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: asset asset_ownerId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset
    ADD CONSTRAINT "asset_ownerId_fkey" FOREIGN KEY ("ownerId") REFERENCES public."user"(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: asset asset_stackId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset
    ADD CONSTRAINT "asset_stackId_fkey" FOREIGN KEY ("stackId") REFERENCES public.stack(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: asset_video asset_video_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.asset_video
    ADD CONSTRAINT "asset_video_assetId_fkey" FOREIGN KEY ("assetId") REFERENCES public.asset(id) ON DELETE CASCADE;


--
-- Name: cluster_group_request cluster_group_request_clusterGroupId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cluster_group_request
    ADD CONSTRAINT "cluster_group_request_clusterGroupId_fkey" FOREIGN KEY ("clusterGroupId") REFERENCES public.cluster_group(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: cluster_group_request cluster_group_request_userId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cluster_group_request
    ADD CONSTRAINT "cluster_group_request_userId_fkey" FOREIGN KEY ("userId") REFERENCES public."user"(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: face_search face_search_faceId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.face_search
    ADD CONSTRAINT "face_search_faceId_fkey" FOREIGN KEY ("faceId") REFERENCES public.asset_face(id) ON DELETE CASCADE;


--
-- Name: integrity_report integrity_report_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.integrity_report
    ADD CONSTRAINT "integrity_report_assetId_fkey" FOREIGN KEY ("assetId") REFERENCES public.asset(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: integrity_report integrity_report_fileAssetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.integrity_report
    ADD CONSTRAINT "integrity_report_fileAssetId_fkey" FOREIGN KEY ("fileAssetId") REFERENCES public.asset_file(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: library library_ownerId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.library
    ADD CONSTRAINT "library_ownerId_fkey" FOREIGN KEY ("ownerId") REFERENCES public."user"(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: memory_asset memory_asset_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.memory_asset
    ADD CONSTRAINT "memory_asset_assetId_fkey" FOREIGN KEY ("assetId") REFERENCES public.asset(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: memory_asset_audit memory_asset_audit_memoryId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.memory_asset_audit
    ADD CONSTRAINT "memory_asset_audit_memoryId_fkey" FOREIGN KEY ("memoryId") REFERENCES public.memory(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: memory_asset memory_asset_memoriesId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.memory_asset
    ADD CONSTRAINT "memory_asset_memoriesId_fkey" FOREIGN KEY ("memoriesId") REFERENCES public.memory(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: memory memory_ownerId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.memory
    ADD CONSTRAINT "memory_ownerId_fkey" FOREIGN KEY ("ownerId") REFERENCES public."user"(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: notification notification_userId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.notification
    ADD CONSTRAINT "notification_userId_fkey" FOREIGN KEY ("userId") REFERENCES public."user"(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: ocr_search ocr_search_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ocr_search
    ADD CONSTRAINT "ocr_search_assetId_fkey" FOREIGN KEY ("assetId") REFERENCES public.asset(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: partner partner_sharedById_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.partner
    ADD CONSTRAINT "partner_sharedById_fkey" FOREIGN KEY ("sharedById") REFERENCES public."user"(id) ON DELETE CASCADE;


--
-- Name: partner partner_sharedWithId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.partner
    ADD CONSTRAINT "partner_sharedWithId_fkey" FOREIGN KEY ("sharedWithId") REFERENCES public."user"(id) ON DELETE CASCADE;


--
-- Name: person person_faceAssetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.person
    ADD CONSTRAINT "person_faceAssetId_fkey" FOREIGN KEY ("faceAssetId") REFERENCES public.asset_face(id) ON DELETE SET NULL;


--
-- Name: person_group person_group_clusterGroupId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.person_group
    ADD CONSTRAINT "person_group_clusterGroupId_fkey" FOREIGN KEY ("clusterGroupId") REFERENCES public.cluster_group(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: person person_ownerId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.person
    ADD CONSTRAINT "person_ownerId_fkey" FOREIGN KEY ("ownerId") REFERENCES public."user"(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: person person_personGroupId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.person
    ADD CONSTRAINT "person_personGroupId_fkey" FOREIGN KEY ("personGroupId") REFERENCES public.person_group(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: plugin_method plugin_method_pluginId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.plugin_method
    ADD CONSTRAINT "plugin_method_pluginId_fkey" FOREIGN KEY ("pluginId") REFERENCES public.plugin(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: session session_parentId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.session
    ADD CONSTRAINT "session_parentId_fkey" FOREIGN KEY ("parentId") REFERENCES public.session(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: session_sync_checkpoint session_sync_checkpoint_sessionId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.session_sync_checkpoint
    ADD CONSTRAINT "session_sync_checkpoint_sessionId_fkey" FOREIGN KEY ("sessionId") REFERENCES public.session(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: session session_userId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.session
    ADD CONSTRAINT "session_userId_fkey" FOREIGN KEY ("userId") REFERENCES public."user"(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: shared_link shared_link_albumId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.shared_link
    ADD CONSTRAINT "shared_link_albumId_fkey" FOREIGN KEY ("albumId") REFERENCES public.album(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: shared_link_asset shared_link_asset_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.shared_link_asset
    ADD CONSTRAINT "shared_link_asset_assetId_fkey" FOREIGN KEY ("assetId") REFERENCES public.asset(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: shared_link_asset shared_link_asset_sharedLinkId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.shared_link_asset
    ADD CONSTRAINT "shared_link_asset_sharedLinkId_fkey" FOREIGN KEY ("sharedLinkId") REFERENCES public.shared_link(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: shared_link shared_link_userId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.shared_link
    ADD CONSTRAINT "shared_link_userId_fkey" FOREIGN KEY ("userId") REFERENCES public."user"(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: smart_search smart_search_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.smart_search
    ADD CONSTRAINT "smart_search_assetId_fkey" FOREIGN KEY ("assetId") REFERENCES public.asset(id) ON DELETE CASCADE;


--
-- Name: stack stack_ownerId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.stack
    ADD CONSTRAINT "stack_ownerId_fkey" FOREIGN KEY ("ownerId") REFERENCES public."user"(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: stack stack_primaryAssetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.stack
    ADD CONSTRAINT "stack_primaryAssetId_fkey" FOREIGN KEY ("primaryAssetId") REFERENCES public.asset(id);


--
-- Name: tag_asset tag_asset_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tag_asset
    ADD CONSTRAINT "tag_asset_assetId_fkey" FOREIGN KEY ("assetId") REFERENCES public.asset(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: tag_asset tag_asset_tagId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tag_asset
    ADD CONSTRAINT "tag_asset_tagId_fkey" FOREIGN KEY ("tagId") REFERENCES public.tag(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: tag_closure tag_closure_id_ancestor_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tag_closure
    ADD CONSTRAINT tag_closure_id_ancestor_fkey FOREIGN KEY (id_ancestor) REFERENCES public.tag(id) ON DELETE CASCADE;


--
-- Name: tag_closure tag_closure_id_descendant_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tag_closure
    ADD CONSTRAINT tag_closure_id_descendant_fkey FOREIGN KEY (id_descendant) REFERENCES public.tag(id) ON DELETE CASCADE;


--
-- Name: tag tag_parentId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tag
    ADD CONSTRAINT "tag_parentId_fkey" FOREIGN KEY ("parentId") REFERENCES public.tag(id) ON DELETE CASCADE;


--
-- Name: tag tag_userId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tag
    ADD CONSTRAINT "tag_userId_fkey" FOREIGN KEY ("userId") REFERENCES public."user"(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: user user_clusterGroupId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public."user"
    ADD CONSTRAINT "user_clusterGroupId_fkey" FOREIGN KEY ("clusterGroupId") REFERENCES public.cluster_group(id) ON UPDATE CASCADE;


--
-- Name: user_metadata user_metadata_userId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.user_metadata
    ADD CONSTRAINT "user_metadata_userId_fkey" FOREIGN KEY ("userId") REFERENCES public."user"(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: video_stream_segment video_stream_segment_variantId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.video_stream_segment
    ADD CONSTRAINT "video_stream_segment_variantId_fkey" FOREIGN KEY ("variantId") REFERENCES public.video_stream_variant(id) ON DELETE CASCADE;


--
-- Name: video_stream_session video_stream_session_assetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.video_stream_session
    ADD CONSTRAINT "video_stream_session_assetId_fkey" FOREIGN KEY ("assetId") REFERENCES public.asset(id) ON DELETE CASCADE;


--
-- Name: video_stream_variant video_stream_variant_sessionId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.video_stream_variant
    ADD CONSTRAINT "video_stream_variant_sessionId_fkey" FOREIGN KEY ("sessionId") REFERENCES public.video_stream_session(id) ON DELETE CASCADE;


--
-- Name: workflow_log workflow_log_workflowId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.workflow_log
    ADD CONSTRAINT "workflow_log_workflowId_fkey" FOREIGN KEY ("workflowId") REFERENCES public.workflow(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: workflow_log workflow_log_workflowStepId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.workflow_log
    ADD CONSTRAINT "workflow_log_workflowStepId_fkey" FOREIGN KEY ("workflowStepId") REFERENCES public.workflow_step(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: workflow workflow_ownerId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.workflow
    ADD CONSTRAINT "workflow_ownerId_fkey" FOREIGN KEY ("ownerId") REFERENCES public."user"(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: workflow_step workflow_step_pluginMethodId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.workflow_step
    ADD CONSTRAINT "workflow_step_pluginMethodId_fkey" FOREIGN KEY ("pluginMethodId") REFERENCES public.plugin_method(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: workflow_step workflow_step_workflowId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.workflow_step
    ADD CONSTRAINT "workflow_step_workflowId_fkey" FOREIGN KEY ("workflowId") REFERENCES public.workflow(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict QHfP56dk7S2LHjpVMI2hzHCHmZIOEYbSzbZZjuvnT3g2EQrEaGNEAIomt7MG2Nu

